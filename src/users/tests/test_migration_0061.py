import importlib

from django.apps import apps
from django.db import connection
from django.test import TestCase

migration_module = importlib.import_module(
    "users.migrations.0061_drop_orphan_grouped_columns",
)


class _SchemaEditorStub:
    """Minimal schema editor that runs SQL on the test connection."""

    connection = connection

    def quote_name(self, name):
        return connection.ops.quote_name(name)

    def execute(self, sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)


def _user_columns():
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                "users_user",
            )
        }


class DropOrphanGroupedColumnsTests(TestCase):
    """Migration 0061 drops orphan *_grouped columns only where they exist."""

    def _run_migration(self):
        migration_module.drop_orphan_columns(apps, _SchemaEditorStub())

    def test_noop_when_columns_absent(self):
        """Running on a clean schema changes nothing."""
        before = _user_columns()
        self._run_migration()
        self.assertEqual(_user_columns(), before)

    def test_drops_existing_orphan_columns(self):
        """Orphan columns present in the table are removed."""
        with connection.cursor() as cursor:
            for column in ("anime_grouped", "tv_grouped"):
                cursor.execute(
                    "ALTER TABLE users_user ADD COLUMN "
                    f"{connection.ops.quote_name(column)} "
                    "boolean NOT NULL DEFAULT false",
                )
        self.assertIn("anime_grouped", _user_columns())

        self._run_migration()

        columns = _user_columns()
        self.assertNotIn("anime_grouped", columns)
        self.assertNotIn("tv_grouped", columns)
        self.assertIn("username", columns)
