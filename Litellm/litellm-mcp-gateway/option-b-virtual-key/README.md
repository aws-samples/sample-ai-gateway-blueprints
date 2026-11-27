# Option B: per-user virtual key (LiteLLM community edition)

For teams without a LiteLLM Enterprise license. Each person gets their own LiteLLM virtual key, and that key identifies them to the gateway. The Atlassian side is the same as in [option A](../option-a-enterprise-idp/): the gateway is the OAuth client, each user consents once (three-legged OAuth, 3LO), and the gateway holds and refreshes each user's Atlassian token, stored under their LiteLLM user.

What you give up compared with option A:

- An administrator creates each user and key, and manages team membership in LiteLLM instead of the IdP.
- Keys are secrets that you distribute and rotate.
- The community edition writes no audit log of administrator changes to keys, teams and users.

## Rules for keys

- **One key per person.** The gateway stores the Atlassian token under the key's user. A key shared between two people, or a team key used by many agents, makes everyone act as one Confluence user.
- **Immutable user IDs.** Use an employee ID or IdP subject as the LiteLLM user ID, not a name that someone else could get later: the stored Atlassian token belongs to the user ID.
- **Only administrators issue keys.** `key_generation_settings` in [`proxy/config.yaml`](proxy/config.yaml) stops users from minting extra keys for themselves, and `upperbound_key_generate_params` caps every key at 30 days (tested).
- **Keys are secrets.** [`scripts/add_user.sh`](scripts/add_user.sh) writes the key to `<user-id>.key` with mode 0600 and never prints it. Hand it over through your normal secret distribution channel, then delete the file.

## Set up

Deploy the infrastructure with `edition = "community"`, store the Atlassian client, and start the gateway, as described in [`infra/terraform/README.md`](../infra/terraform/README.md#deploy). Run the commands below from this folder (`option-b-virtual-key/`).

1. **Create the teams** that cap which tools their members can use. Use an administrator key; the master key is for the first setup only.
   ```bash
   export LITELLM_PROXY_URL=https://litellm.internal.example.com
   export LITELLM_ADMIN_KEY="$(aws secretsmanager get-secret-value \
     --secret-id "$(terraform -chdir=../infra/terraform output -raw master_key_secret_arn)" \
     --query SecretString --output text)"
   ../scripts/create_team.sh confluence-read
   ```
   With no tool list, the team gets the five read tools of the server. Pass tool names to narrow it. The script also sets request and token rate limits per team (`TEAM_RPM_LIMIT`, `TEAM_TPM_LIMIT`) and an optional budget (`TEAM_MAX_BUDGET`).
2. **Create each user and their key.** For the tests below, create two users:
   ```bash
   ./scripts/add_user.sh e1001 carol@example.com confluence-read 30d
   ./scripts/add_user.sh e1002 dave@example.com confluence-read 30d
   unset LITELLM_ADMIN_KEY
   ```
3. **Each user connects Atlassian once,** with their own key. The user installs the key file they received, then runs the shared helpers:
   ```bash
   mkdir -p ~/.config/litellm
   install -m 600 e1001.key ~/.config/litellm/credential && rm e1001.key
   export GATEWAY_URL=https://litellm.internal.example.com
   export GATEWAY_CREDENTIAL_FILE=~/.config/litellm/credential
   python3 ../scripts/connect_atlassian.py
   python3 ../scripts/check_access.py
   ```
   `connect_atlassian.py` opens the Atlassian consent page, asks for the address the browser lands on after approval, then shows the Atlassian account and sites that were connected and asks the user to confirm them. `check_access.py` lists the tools of the user's team and calls `atlassianUserInfo`, which must return the user's own Atlassian account.
4. **Run the sample agent:**
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r client/requirements.txt
   export LITELLM_PROXY_URL=https://litellm.internal.example.com
   export USER_VIRTUAL_KEY="$(cat ~/.config/litellm/credential)"
   python3 client/agent.py
   ```

## Rotate a key

Issue the new key before the old one expires, then delete the old one. The stored Atlassian token belongs to the user, not the key, so the user does not consent again (tested).

```bash
(umask 077; printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$LITELLM_ADMIN_KEY" > admin.headers)
(umask 077; curl -fsS -X POST "$LITELLM_PROXY_URL/key/generate" -H @admin.headers \
  -d '{"user_id": "e1001", "team_id": "confluence-read", "duration": "30d", "key_alias": "e1001-30d-2"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["key"])' > e1001.key)
curl -fsS -X POST "$LITELLM_PROXY_URL/key/delete" -H @admin.headers -d '{"key_aliases": ["e1001-30d-1"]}'
rm admin.headers
```

A deleted key can keep working for up to 60 seconds on gateway tasks that cached it.

## Offboard a user

Delete the LiteLLM user, which also deletes their keys, then delete their stored Atlassian token as described in [offboarding](../README.md#security-notes):

```bash
(umask 077; printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$LITELLM_ADMIN_KEY" > admin.headers)
curl -fsS -X POST "$LITELLM_PROXY_URL/user/delete" -H @admin.headers -d '{"user_ids": ["e1001"]}'
rm admin.headers
```

The admin commands send the key from a file with mode 0600, so it does not appear in the process list.

## Hosted agents

A hosted agent (for example on Amazon Bedrock AgentCore Runtime) needs the calling user's key on every request. Keep keys in a secret store indexed by the signed-in user (for example AWS Secrets Manager), resolve the key from the user identity that your agent runtime already validated, and pass it to `ask()` in [`client/agent.py`](client/agent.py). Never fall back to a shared key. If you already run an IdP for the agent, option A removes this mapping.

## Admin UI sign-in

LiteLLM Admin UI SSO is [free for up to 5 users](https://docs.litellm.ai/docs/proxy/admin_ui_sso). In LiteLLM 1.109.1 that limit counts every LiteLLM user, including the users you create here, so a deployment with more than 5 users needs a license for SSO. Without SSO, administrators use their own administrator keys; keep the master key as break-glass only.

## What to test

| Check | Expected |
|---|---|
| carol (`e1001`, consented) lists tools | Her team's tools only, prefixed `atlassian_confluence-` |
| dave (`e1002`, no consent) lists tools | `auth_required` for the server |
| carol calls a tool outside the team's list | Rejected by the gateway |
| carol tries to create a key (`POST /key/generate`) | Rejected: only administrators issue keys |
| Expired or deleted key | HTTP 401 |
| Key rotated | New key works without new consent; old key gets HTTP 401 |
| LiteLLM spend logs | Each MCP call with carol's user ID and team |
