#!/usr/bin/env bash
# Set up an ENCRYPTED archive (rclone crypt), and prove it round-trips.
#
# WHY
# Your users/pokemon/trades tables contain Telegram user IDs and usernames —
# real personal data about real players. The bucket is the least-monitored
# surface in the whole system. Client-side encryption means a leaked bucket is
# noise rather than a player-data breach.
#
# ⚠⚠ THE KEY-LOSS WARNING — READ IT PROPERLY ⚠⚠
#
# rclone crypt has no key escrow, no recovery, no reset. If you lose the
# password, every byte in the archive is permanently unreadable. There is no
# support ticket that fixes this.
#
# Encryption converts "someone might read my backups" into "I might lose my
# backups entirely". That is a GOOD trade only if the password is genuinely
# stored somewhere durable and off this server. If you would not bet your
# players' data on your password manager, do not enable this.
#
# This script refuses to finish until you have confirmed the password is
# stored externally, and it verifies a full encrypt -> upload -> download ->
# decrypt cycle before declaring success.
#
#   ops/encrypt_setup.sh

SCRIPT_NAME=encrypt_setup
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cat <<'BANNER'

════════════════════════════════════════════════════════════════
 ENCRYPTED ARCHIVE SETUP
════════════════════════════════════════════════════════════════

 What you gain: a leaked bucket exposes nothing. Player Telegram
 IDs and usernames are unreadable without your password.

 What you risk: if you lose the password, ALL backups become
 permanently unrecoverable. There is no recovery path. None.

 Before continuing, you must have the password stored somewhere
 that survives this server being destroyed — a password manager,
 not a file on this box, not only in your head.

════════════════════════════════════════════════════════════════

BANNER

read -r -p "Is your encryption password stored OFF this server, durably? [type YES] " c
[[ "$c" == "YES" ]] || { echo "aborted — store the password first, then re-run"; exit 1; }

command -v rclone >/dev/null || die "rclone is not installed"

# ---------------------------------------------------------------------------
# Guide, rather than generate: the password must be chosen and recorded by a
# human. Generating one here and printing it invites it being lost in scrollback.
# ---------------------------------------------------------------------------
cat <<EOF

Configure the crypt remotes interactively. For EACH existing remote you want
encrypted, run:

  rclone --config $RCLONE_CONF config

  → n) New remote
  → name: b2crypt
  → storage: crypt
  → remote: b2:telemon-archive
  → filename_encryption: standard
  → directory_name_encryption: true
  → password: <your password>       (choose your own; record it FIRST)
  → password2: <salt>               (optional but recommended)

Repeat for r2crypt -> r2:telemon-archive.

Then set in ops/backup.env:

  BACKUP_REMOTES="b2crypt: r2crypt:"

EOF

read -r -p "Press Enter once the crypt remotes are configured and BACKUP_REMOTES is updated... " _

# ---------------------------------------------------------------------------
# Re-read config and verify the crypt remotes actually work end to end
# ---------------------------------------------------------------------------
# shellcheck disable=SC1091
source "$OPS_DIR/backup.env"

CRYPT_REMOTES="$BACKUP_REMOTES"
log "verifying: $CRYPT_REMOTES"

# ---------------------------------------------------------------------------
# Assert these are ACTUALLY crypt remotes before testing anything.
#
# Without this the whole verification is theatre: a plain b2:/r2: remote passes
# the round-trip trivially (plaintext uploads and downloads fine), and the
# at-rest check below silently skips because a non-crypt remote has no
# underlying `remote =` key to resolve. The script would then print
# "ENCRYPTION VERIFIED" over an archive with no encryption at all.
# ---------------------------------------------------------------------------
for R in $CRYPT_REMOTES; do
  RNAME="${R%%:*}"
  RTYPE="$(rclone --config "$RCLONE_CONF" config show "$RNAME" 2>/dev/null | awk -F' = ' '/^type/{print $2; exit}')"
  if [[ "$RTYPE" != "crypt" ]]; then
    die "$RNAME is type '${RTYPE:-unknown}', not 'crypt'. BACKUP_REMOTES in ops/backup.env still points at unencrypted remotes, so there is nothing to verify. Configure the crypt remotes and update BACKUP_REMOTES first — do NOT treat this run as proof of encryption."
  fi
  log "  $RNAME: type=crypt ✓"
done

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
CANARY="telemon-encryption-canary-$(stamp)"
echo "$CANARY" > "$WORK/canary.txt"

FAILED=0
for R in $CRYPT_REMOTES; do
  log "── $R"

  # 1. Upload through the crypt layer
  if ! rc copy "$WORK/canary.txt" "${R}/_verify" --no-traverse 2>&1; then
    log "  ✗ upload failed"; FAILED=1; continue
  fi
  log "  ✓ uploaded"

  # 2. Download back through the crypt layer and confirm the plaintext matches
  rm -f "$WORK/roundtrip.txt"
  if ! rc copyto "${R}/_verify/canary.txt" "$WORK/roundtrip.txt" 2>&1; then
    log "  ✗ download failed"; FAILED=1; continue
  fi
  if [[ "$(cat "$WORK/roundtrip.txt")" == "$CANARY" ]]; then
    log "  ✓ decrypted correctly (round-trip verified)"
  else
    log "  ✗ decrypted content does NOT match — the crypt config is wrong"
    FAILED=1; continue
  fi

  # 3. Confirm it is genuinely encrypted at rest. This is the check that
  #    matters: read the SAME object through the UNDERLYING remote (bypassing
  #    the crypt layer) and prove the plaintext is absent. A round-trip alone
  #    proves only that rclone is self-consistent, not that anything was
  #    actually encrypted.
  BASE="$(rclone --config "$RCLONE_CONF" config show "${R%%:*}" 2>/dev/null | awk -F' = ' '/^remote/{print $2; exit}')"
  if [[ -n "$BASE" ]]; then
    # Filenames are encrypted too, so we cannot address the object by name on
    # the underlying remote — list the prefix and grep every object's bytes.
    FOUND_PLAINTEXT=0
    while IFS= read -r obj; do
      [[ -z "$obj" ]] && continue
      if rclone --config "$RCLONE_CONF" cat "${BASE}/${obj}" 2>/dev/null | grep -q "$CANARY"; then
        FOUND_PLAINTEXT=1; break
      fi
    done < <(rclone --config "$RCLONE_CONF" lsf "$BASE" --recursive 2>/dev/null | head -50)

    if [[ "$FOUND_PLAINTEXT" -eq 1 ]]; then
      log "  ✗ PLAINTEXT IS READABLE on the underlying remote — encryption is NOT active"
      FAILED=1
    else
      log "  ✓ ciphertext at rest (canary not readable via ${BASE})"
      # Encrypted filenames are the other half of the guarantee.
      SAMPLE="$(rclone --config "$RCLONE_CONF" lsf "$BASE" --recursive 2>/dev/null | head -1)"
      [[ -n "$SAMPLE" && "$SAMPLE" != *"canary.txt"* ]] \
        && log "  ✓ filenames encrypted (e.g. ${SAMPLE:0:40})" \
        || log "  ~ filename encryption could not be confirmed"
    fi
  else
    log "  ✗ could not resolve the underlying remote for ${R%%:*} — at-rest encryption UNVERIFIED"
    FAILED=1
  fi
done

if [[ "$FAILED" -eq 1 ]]; then
  die "encryption verification FAILED. Do NOT rely on this archive until the round-trip passes."
fi

cat <<EOF

════════════════════════════════════════════════════════════════
 ✅ ENCRYPTION VERIFIED
════════════════════════════════════════════════════════════════
 Encrypt -> upload -> download -> decrypt round-trip passed, and
 the plaintext is confirmed unreadable on the underlying remote.

 ⚠ Existing UNENCRYPTED artifacts are still in the old prefixes.
   They are not retroactively encrypted. Either delete them once
   the encrypted archive has a full base backup + drill, or accept
   that older backups remain in the clear.

 NEXT:
   ops/basebackup.sh        # write a base backup through the crypt layer
   ops/restore_drill.sh     # prove recovery works ENCRYPTED
════════════════════════════════════════════════════════════════

EOF

alert "🔐 Telemon archive encryption verified (round-trip + at-rest check passed). Run ops/basebackup.sh then ops/restore_drill.sh to prove recovery works encrypted."
