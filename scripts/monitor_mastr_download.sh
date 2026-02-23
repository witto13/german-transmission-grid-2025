#!/bin/bash
# Monitor MaStR download progress

echo "MaStR Download Monitor"
echo "======================"
echo ""

# Check if process is running
if ps aux | grep download_complete_mastr.py | grep -v grep > /dev/null; then
    echo "✓ Download process is RUNNING"
else
    echo "✗ Download process is NOT running"
fi

echo ""
echo "Latest log entries:"
echo "-------------------"
tail -20 /root/egon_2025_project/mastr_complete_download.log

echo ""
echo "Database size:"
echo "--------------"
if [ -f ~/.open-MaStR/data/sqlite/open-mastr.db ]; then
    ls -lh ~/.open-MaStR/data/sqlite/open-mastr.db
else
    echo "Database not yet created"
fi

echo ""
echo "Monitor live with:"
echo "  tail -f /root/egon_2025_project/mastr_complete_download.log"
