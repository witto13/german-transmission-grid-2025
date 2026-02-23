#!/bin/bash
# Monitor MaStR PostgreSQL import progress

echo "MaStR PostgreSQL Import Monitor"
echo "================================"
echo ""

# Check if process is running
if ps aux | grep import_mastr_to_postgresql.py | grep -v grep > /dev/null; then
    echo "✓ Import process is RUNNING"
    ps aux | grep import_mastr_to_postgresql.py | grep -v grep | awk '{print "  CPU: "$3"%  Memory: "$4"%  Time: "$10}'
else
    echo "✗ Import process is NOT running"
fi

echo ""
echo "Latest log entries:"
echo "-------------------"
tail -30 /root/egon_2025_project/mastr_import_to_postgresql.log 2>/dev/null || echo "Log file not found"

echo ""
echo "PostgreSQL mastr schema status:"
echo "-------------------------------"
docker exec -e PGPASSWORD=data egon-data-local-database-container psql -U egon -d egon-data -c "
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'mastr'
ORDER BY tablename
LIMIT 10;" 2>/dev/null || echo "Schema not yet created or no tables"

echo ""
echo "Monitor live with:"
echo "  tail -f /root/egon_2025_project/mastr_import_to_postgresql.log"
