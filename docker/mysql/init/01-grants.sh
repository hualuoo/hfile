# Runs once, when the MySQL data volume is first initialised (scripts/dev-db.sh). The image's
# entrypoint sources this file (it must NOT be executable), which provides docker_process_sql and
# the MYSQL_* variables.
#
# The app user may use every database whose name starts with hfile (hfile, hfile_platform, ...),
# and Django's test databases of them (test_hfile, test_hfile_platform, ...), which it creates and
# drops itself. In a database name in GRANT, % and _ are wildcards; \_ is a plain underscore.
docker_process_sql --database=mysql <<EOSQL
GRANT ALL PRIVILEGES ON \`hfile%\`.* TO '${MYSQL_USER}'@'%';
GRANT ALL PRIVILEGES ON \`test\\_hfile%\`.* TO '${MYSQL_USER}'@'%';
EOSQL
