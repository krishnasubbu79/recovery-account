#!/usr/bin/env bash
# Renders the recovery-account artifacts:
#   $OUT_DIR/delegated-admin   -> IAM policies and the Recovery-ManageFoundation document
#   $OUT_DIR/recovery-account  -> the one-time bootstrap template
#
#   source deployment.env && scripts/render.sh
#
# The foundation template is embedded in the document and pinned by SHA-256,
# so the runbook can only ever deploy the approved template. No AWS calls.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/cloudformation/foundation.yaml"
DOCUMENT="$ROOT/ssm/manage-foundation.yaml"
MARKER='        __FOUNDATION_TEMPLATE_BODY__'
MAX_DOCUMENT_BYTES=65536

fail() { printf 'render: %s\n' "$*" >&2; exit 1; }

require() {
  local name="$1" pattern="$2" value="${!1-}"
  [[ "$value" =~ $pattern ]] || fail "$name is missing or invalid"
}

require RECOVERY_REGION '^[a-z]{2}-[a-z]+-[0-9]$'
require DELEGATED_ADMIN_ACCOUNT_ID '^[0-9]{12}$'
require RECOVERY_ACCOUNT_ID '^[0-9]{12}$'
require OUT_DIR '^/.+'
[[ "$DELEGATED_ADMIN_ACCOUNT_ID" != "$RECOVERY_ACCOUNT_ID" ]] || fail "Delegated Admin and the recovery account must differ"
grep -q -- '{{' "$TEMPLATE" && fail "the template must not contain double braces; SSM would read them as references"
[[ "$(tail -c 1 "$TEMPLATE" | od -An -c | tr -d ' ')" == '\n' ]] || fail "the template must end with exactly one newline"
[[ -z "$(tail -c 2 "$TEMPLATE" | tr -d '\n')" ]] && fail "the template must not end with a blank line"
grep -q "^$MARKER\$" "$DOCUMENT" || fail "the document has no template marker"

TEMPLATE_SHA256="$(shasum -a 256 "$TEMPLATE" | awk '{print $1}')"

render() {
  sed \
    -e "s|DELEGATED_ADMIN_ACCOUNT_ID|$DELEGATED_ADMIN_ACCOUNT_ID|g" \
    -e "s|RECOVERY_ACCOUNT_ID|$RECOVERY_ACCOUNT_ID|g" \
    -e "s|RECOVERY_REGION|$RECOVERY_REGION|g" \
    -e "s|APPROVED_TEMPLATE_SHA256|$TEMPLATE_SHA256|g" \
    "$1"
}

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/delegated-admin" "$OUT_DIR/recovery-account"
chmod 700 "$OUT_DIR"

for source in "$ROOT"/iam/*.json; do
  render "$source" > "$OUT_DIR/delegated-admin/$(basename "$source")"
done
cp "$ROOT/cloudformation/recovery-account-bootstrap.yaml" "$OUT_DIR/recovery-account/"

# Substitute placeholders first, then embed the template verbatim (indented
# under TemplateBody) so its contents are never rewritten by sed.
render "$DOCUMENT" | awk -v marker="$MARKER" -v template="$TEMPLATE" '
  $0 == marker {
    while ((getline line < template) > 0) print (line == "" ? "" : "        " line)
    next
  }
  { print }
' > "$OUT_DIR/delegated-admin/manage-foundation.yaml"

if grep -n -E 'DELEGATED_ADMIN_ACCOUNT_ID|RECOVERY_ACCOUNT_ID|RECOVERY_REGION|APPROVED_TEMPLATE_SHA256|__FOUNDATION_TEMPLATE_BODY__' \
     "$OUT_DIR"/delegated-admin/*; then
  fail "unreplaced placeholders remain (listed above)"
fi
for policy in "$OUT_DIR"/delegated-admin/*.json; do
  jq -e . "$policy" >/dev/null || fail "$policy is not valid JSON"
done
size="$(wc -c < "$OUT_DIR/delegated-admin/manage-foundation.yaml")"
(( size <= MAX_DOCUMENT_BYTES )) || fail "rendered document is $size bytes; SSM allows $MAX_DOCUMENT_BYTES"

printf 'render: foundation template SHA-256 %s, document %s bytes, output in %s\n' "$TEMPLATE_SHA256" "$size" "$OUT_DIR"
