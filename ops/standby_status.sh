#!/usr/bin/env bash
# Report the delayed standby's state: frozen, or how far behind it is.
#
# A standby that has silently stopped replaying is worse than no standby at
# all, because you will reach for it during an incident and find it stale.
# This is what the every-20-minutes timer runs.
#
#   ops/standby_status.sh            human readable
#   ops/standby_status.sh --quiet    only speak up on problems (exit 1)
#
# Thin wrapper: the logic lives in standby_resume.sh, which reports when
# invoked in status mode and only resumes when invoked directly.

exec "$(dirname "${BASH_SOURCE[0]}")/standby_resume.sh" "${1:---status}"
