#!/usr/bin/env python3
import os
import sys
import multiprocessing

from argparse import Namespace
from typing import Union
from functools import partial
from datetime import datetime

from ..logger import Logger
from .commons import utils
from .commons.tap_postgres import FastSyncTapPostgres
from .commons.target_redshift import FastSyncTargetRedshift

LOGGER = Logger().get_logger(__name__)

REQUIRED_CONFIG_KEYS = {
    'tap': ['host', 'port', 'user', 'password'],
    'target': ['host', 'port', 'user', 'password', 'dbname', 's3_bucket'],
}

DEFAULT_VARCHAR_LENGTH = 10000
SHORT_VARCHAR_LENGTH = 256
LONG_VARCHAR_LENGTH = 65535

LOCK = multiprocessing.Lock()


def tap_type_to_target_type(pg_type):
    """Data type mapping from MySQL to Redshift"""
    return {
        'char': 'CHARACTER VARYING({})'.format(DEFAULT_VARCHAR_LENGTH),
        'character': 'CHARACTER VARYING({})'.format(DEFAULT_VARCHAR_LENGTH),
        'varchar': 'CHARACTER VARYING({})'.format(DEFAULT_VARCHAR_LENGTH),
        'character varying': 'CHARACTER VARYING({})'.format(DEFAULT_VARCHAR_LENGTH),
        'text': 'CHARACTER VARYING({})'.format(LONG_VARCHAR_LENGTH),
        'bit': 'BOOLEAN',
        'varbit': 'NUMERIC NULL',
        'bit varying': 'NUMERIC NULL',
        'smallint': 'NUMERIC NULL',
        'int': 'NUMERIC NULL',
        'integer': 'NUMERIC NULL',
        'bigint': 'NUMERIC NULL',
        'smallserial': 'NUMERIC NULL',
        'serial': 'NUMERIC NULL',
        'bigserial': 'NUMERIC NULL',
        'numeric': 'FLOAT',
        'double precision': 'FLOAT',
        'real': 'FLOAT',
        'bool': 'BOOLEAN',
        'boolean': 'BOOLEAN',
        'date': 'TIMESTAMP WITHOUT TIME ZONE',
        'timestamp': 'TIMESTAMP WITHOUT TIME ZONE',
        'timestamp without time zone': 'TIMESTAMP WITHOUT TIME ZONE',
        'timestamp with time zone': 'TIMESTAMP WITHOUT TIME ZONE',
        'time': 'CHARACTER VARYING({})'.format(SHORT_VARCHAR_LENGTH),
        'time without time zone': 'CHARACTER VARYING({})'.format(SHORT_VARCHAR_LENGTH),
        'time with time zone': 'CHARACTER VARYING({})'.format(SHORT_VARCHAR_LENGTH),
        # ARRAY is all uppercase, because postgres stores it in this format in information_schema.columns.data_type
        'ARRAY': 'CHARACTER VARYING({})'.format(LONG_VARCHAR_LENGTH),
        'json': 'CHARACTER VARYING({})'.format(LONG_VARCHAR_LENGTH),
        'jsonb': 'CHARACTER VARYING({})'.format(LONG_VARCHAR_LENGTH),
    }.get(pg_type, 'CHARACTER VARYING({})'.format(DEFAULT_VARCHAR_LENGTH))


# pylint: disable=too-many-locals
def sync_table(table: str, args: Namespace) -> Union[bool, str]:
    """Sync one table"""
    postgres = FastSyncTapPostgres(args.tap, tap_type_to_target_type)
    redshift = FastSyncTargetRedshift(args.target, args.transform)

    try:
        dbname = args.tap.get('dbname')
        filename = utils.gen_export_filename(
            tap_id=args.target.get('tap_id'), table=table
        )
        filepath = os.path.join(args.temp_dir, filename)
        target_schema = utils.get_target_schema(args.target, table)

        # Open connection
        postgres.open_connection()

        # Get bookmark - LSN position or Incremental Key value
        bookmark = utils.get_bookmark_for_table(
            table, args.properties, postgres, dbname=dbname
        )

        # Exporting table data, get table definitions and close connection to avoid timeouts
        postgres.copy_table(table, filepath)
        size_bytes = os.path.getsize(filepath)
        redshift_types = postgres.map_column_types_to_target(table)
        redshift_columns = redshift_types.get('columns', [])
        primary_key = redshift_types.get('primary_key')
        postgres.close_connection()

        # Uploading to S3
        s3_key = redshift.upload_to_s3(filepath)
        os.remove(filepath)

        # Creating temp table in Redshift
        redshift.drop_table(target_schema, table, is_temporary=True)
        redshift.create_table(
            target_schema, table, redshift_columns, primary_key, is_temporary=True
        )

        # Load into Redshift table
        redshift.copy_to_table(
            s3_key, target_schema, table, size_bytes, is_temporary=True
        )

        # Obfuscate columns
        redshift.obfuscate_columns(target_schema, table)

        # Create target table and swap with the temp table in Redshift
        redshift.swap_tables(target_schema, table)

        # Save bookmark to singer state file
        # Lock to ensure that only one process writes the same state file at a time
        LOCK.acquire()
        try:
            utils.save_state_file(args.state, table, bookmark, dbname=dbname)
        finally:
            LOCK.release()

        # Table loaded, grant select on all tables in target schema
        grantees = utils.get_grantees(args.target, table)
        utils.grant_privilege(target_schema, grantees, redshift.grant_usage_on_schema)
        utils.grant_privilege(target_schema, grantees, redshift.grant_select_on_schema)

        return True

    except Exception as exc:
        LOGGER.exception(exc)
        return '{}: {}'.format(table, exc)


def main_impl():
    """Main sync logic"""
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    pool_size = utils.get_pool_size(args.tap)
    start_time = datetime.now()
    table_sync_excs = []

    # Log start info
    LOGGER.info(
        """
        -------------------------------------------------------
        STARTING SYNC
        -------------------------------------------------------
            Tables selected to sync        : %s
            Total tables selected to sync  : %s
            Pool size                      : %s
        -------------------------------------------------------
        """,
        args.tables,
        len(args.tables),
        pool_size,
    )

    # if internal arg drop_pg_slot is set to True, then we drop the slot before starting resync
    if args.drop_pg_slot:
        FastSyncTapPostgres.drop_slot(args.tap)

    # Create target schemas sequentially, Redshift doesn't like it running in parallel
    redshift = FastSyncTargetRedshift(args.target, args.transform)
    redshift.create_schemas(args.tables)

    # Start loading tables in parallel in spawning processes
    with multiprocessing.Pool(pool_size) as proc:
        table_sync_excs = list(
            filter(
                lambda x: not isinstance(x, bool),
                proc.map(partial(sync_table, args=args), args.tables),
            )
        )

    # Log summary
    end_time = datetime.now()
    LOGGER.info(
        """
        -------------------------------------------------------
        SYNC FINISHED - SUMMARY
        -------------------------------------------------------
            Total tables selected to sync  : %s
            Tables loaded successfully     : %s
            Exceptions during table sync   : %s

            Pool size                      : %s
            Runtime                        : %s
        -------------------------------------------------------
        """,
        len(args.tables),
        len(args.tables) - len(table_sync_excs),
        str(table_sync_excs),
        pool_size,
        end_time - start_time,
    )

    if len(table_sync_excs) > 0:
        sys.exit(1)


def main():
    """Main entry point"""
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc
