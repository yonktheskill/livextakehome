"""Initial schema migration; applied transactionally by app.db.database.initialize.

Version 1 is the explicit, reviewed initial TABLES schema. Future migrations must be
sequential functions using playhouse.migrate for field/index changes; create_tables
is not used to pretend an existing table has been upgraded.
"""
VERSION = 1
