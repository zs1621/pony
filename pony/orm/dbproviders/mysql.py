from decimal import Decimal, InvalidOperation
from datetime import datetime, date, time, timedelta
from uuid import UUID

import warnings
warnings.filterwarnings('ignore', '^Table.+already exists$', Warning, '^pony\\.orm\\.dbapiprovider$')

import MySQLdb
import MySQLdb.converters
from MySQLdb.constants import FIELD_TYPE, FLAG, CLIENT

from pony.orm import core, dbschema, dbapiprovider
from pony.orm.core import log_orm, log_sql, OperationalError
from pony.orm.dbapiprovider import DBAPIProvider, Pool, get_version_tuple
from pony.orm.sqltranslation import SQLTranslator
from pony.orm.sqlbuilding import SQLBuilder, join
from pony.utils import throw

class MySQLColumn(dbschema.Column):
    auto_template = '%(type)s PRIMARY KEY AUTO_INCREMENT'

class MySQLSchema(dbschema.DBSchema):
    dialect = 'MySQL'
    inline_fk_syntax = False
    column_class = MySQLColumn

class MySQLTranslator(SQLTranslator):
    dialect = 'MySQL'

class MySQLBuilder(SQLBuilder):
    dialect = 'MySQL'
    def CONCAT(builder, *args):
        return 'concat(',  join(', ', map(builder, args)), ')'
    def TRIM(builder, expr, chars=None):
        if chars is None: return 'trim(', builder(expr), ')'
        return 'trim(both ', builder(chars), ' from ' ,builder(expr), ')'
    def LTRIM(builder, expr, chars=None):
        if chars is None: return 'ltrim(', builder(expr), ')'
        return 'trim(leading ', builder(chars), ' from ' ,builder(expr), ')'
    def RTRIM(builder, expr, chars=None):
        if chars is None: return 'rtrim(', builder(expr), ')'
        return 'trim(trailing ', builder(chars), ' from ' ,builder(expr), ')'
    def YEAR(builder, expr):
        return 'year(', builder(expr), ')'
    def MONTH(builder, expr):
        return 'month(', builder(expr), ')'
    def DAY(builder, expr):
        return 'day(', builder(expr), ')'
    def HOUR(builder, expr):
        return 'hour(', builder(expr), ')'
    def MINUTE(builder, expr):
        return 'minute(', builder(expr), ')'
    def SECOND(builder, expr):
        return 'second(', builder(expr), ')'

def _string_sql_type(converter):
    db_encoding = converter.db_encoding or 'utf8'
    if converter.max_len:
        return 'VARCHAR(%d) CHARACTER SET %s' % (converter.max_len, db_encoding)
    return 'LONGTEXT CHARACTER SET %s' % db_encoding

class MySQLUnicodeConverter(dbapiprovider.UnicodeConverter):
    sql_type = _string_sql_type

class MySQLStrConverter(dbapiprovider.StrConverter):
    sql_type = _string_sql_type

class MySQLLongConverter(dbapiprovider.IntConverter):
    def sql_type(converter):
        return 'BIGINT'

class MySQLRealConverter(dbapiprovider.RealConverter):
    def sql_type(converter):
        return 'DOUBLE'

class MySQLBlobConverter(dbapiprovider.BlobConverter):
    def sql_type(converter):
        return 'LONGBLOB'

class MySQLUuidConverter(dbapiprovider.UuidConverter):
    def sql_type(converter):
        return 'BINARY(16)'

class MySQLProvider(DBAPIProvider):
    dialect = 'MySQL'
    paramstyle = 'format'
    quote_char = "`"
    max_name_len = 64
    table_if_not_exists_syntax = True
    index_if_not_exists_syntax = False
    select_for_update_nowait_syntax = False
    max_time_precision = default_time_precision = 0

    dbapi_module = MySQLdb
    dbschema_cls = MySQLSchema
    translator_cls = MySQLTranslator
    sqlbuilder_cls = MySQLBuilder

    converter_classes = [
        (bool, dbapiprovider.BoolConverter),
        (unicode, MySQLUnicodeConverter),
        (str, MySQLStrConverter),
        (int, dbapiprovider.IntConverter),
        (long, MySQLLongConverter),
        (float, MySQLRealConverter),
        (Decimal, dbapiprovider.DecimalConverter),
        (buffer, MySQLBlobConverter),
        (datetime, dbapiprovider.DatetimeConverter),
        (date, dbapiprovider.DateConverter),
        (UUID, MySQLUuidConverter),
    ]

    def inspect_connection(provider, connection):
        cursor = connection.cursor()
        cursor.execute('select version()')
        row = cursor.fetchone()
        assert row is not None
        provider.server_version = get_version_tuple(row[0])
        if provider.server_version >= (5, 6, 4):
            provider.max_time_precision = 6
        cursor.execute('select database()')
        provider.default_schema_name = cursor.fetchone()[0]

    def should_reconnect(provider, exc):
        return isinstance(exc, MySQLdb.OperationalError) and exc.args[0] == 2006

    def get_pool(provider, *args, **kwargs):
        if 'conv' not in kwargs:
            conv = MySQLdb.converters.conversions.copy()
            conv[FIELD_TYPE.BLOB] = [(FLAG.BINARY, buffer)]
            conv[FIELD_TYPE.TIMESTAMP] = str2datetime
            conv[FIELD_TYPE.DATETIME] = str2datetime
            conv[FIELD_TYPE.TIME] = str2timedelta
            kwargs['conv'] = conv
        if 'charset' not in kwargs:
            kwargs['charset'] = 'utf8'
        kwargs['client_flag'] = kwargs.get('client_flag', 0) | CLIENT.FOUND_ROWS 
        return Pool(MySQLdb, *args, **kwargs)

    def table_exists(provider, connection, table_name):
        db_name, table_name = provider.split_table_name(table_name)
        cursor = connection.cursor()
        cursor.execute('SELECT 1 FROM information_schema.tables '
                       'WHERE table_schema=%s and table_name=%s',
                       [ db_name, table_name ])
        return cursor.fetchone() is not None

    def index_exists(provider, connection, table_name, index_name):
        db_name, table_name = provider.split_table_name(table_name)
        cursor = connection.cursor()
        cursor.execute('SELECT 1 FROM information_schema.statistics '
                       'WHERE table_schema=%s and table_name=%s and index_name=%s',
                       [ db_name, table_name, index_name ])
        return cursor.fetchone() is not None

    def fk_exists(provider, connection, table_name, fk_name):
        db_name, table_name = provider.split_table_name(table_name)
        cursor = connection.cursor()
        cursor.execute('SELECT 1 FROM information_schema.table_constraints '
                       'WHERE table_schema=%s and table_name=%s '
                       "and constraint_type='FOREIGN KEY' and constraint_name=%s",
                       [ db_name, table_name, fk_name ])
        return cursor.fetchone() is not None

    def disable_fk_checks_if_necessary(provider, connection):
        cursor = connection.cursor()
        cursor.execute("SHOW VARIABLES LIKE 'foreign_key_checks'")
        fk = cursor.fetchone()
        if fk is not None: fk = (fk[1] == 'ON')
        if fk: cursor.execute('SET foreign_key_checks = 0')
        return bool(fk)

    def enable_fk_checks_if_necessary(provider, connection, fk):
        assert type(fk) is bool, fk
        if fk:
            cursor = connection.cursor()
            cursor.execute('SET foreign_key_checks = 1')

provider_cls = MySQLProvider

def str2datetime(s):
    if 19 < len(s) < 26: s += '000000'[:26-len(s)]
    s = s.replace('-', ' ').replace(':', ' ').replace('.', ' ').replace('T', ' ')
    return datetime(*map(int, s.split()))

def str2timedelta(s):
    if '.' in s:
        s, fractional = s.split('.')
        microseconds = int((fractional + '000000')[:6])
    else: microseconds = 0
    h, m, s = map(int, s.split(':'))
    td = timedelta(hours=abs(h), minutes=m, seconds=s, microseconds=microseconds)
    return -td if h < 0 else td
