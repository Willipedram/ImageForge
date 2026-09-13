## Summary

- 

## Safety impact

- [ ] No credentials, ProjectData, logs, backups, databases, or user images are committed.
- [ ] Destructive operations have verified backups and recovery tests.
- [ ] Database changes are structure-aware and dry-run reviewed.
- [ ] UI remains responsive and large collections remain bounded.

## Testing

- [ ] `pytest -q`
- [ ] `python -m compileall -q app tests`
- [ ] `python scripts/verify_release.py`

