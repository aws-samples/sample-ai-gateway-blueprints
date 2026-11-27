# Worked example: Amazon Cognito as the identity provider (option A)

Option A works with any OIDC IdP that issues access tokens with a fixed audience and a groups claim. This example uses an [Amazon Cognito user pool](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools.html), tested end to end in November 2026 with LiteLLM proxy 1.109.1 and the [AgentCore agent](../../agentcore/). In production you would usually federate the pool with your company IdP; the gateway side stays the same.

All values below are fictitious: Region `eu-west-1`, pool `eu-west-1_EXAMPLE`, domain prefix `example-gateway-idp`, gateway `https://litellm.internal.example.com`.

## How the token fits the gateway

| Gateway needs | Cognito provides |
|---|---|
| A fixed audience | A [resource server](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html) whose identifier is the gateway URL. The client asks for it with the `resource` parameter (resource binding, also called resource indicators; Essentials or Plus tier), and the access token then carries `aud = https://litellm.internal.example.com`. |
| A stable user ID | `sub`. Cognito access tokens carry no `email`, so the gateway maps users by `sub` only. |
| Groups | `cognito:groups`, with the [group](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-user-groups.html) names. |

## Create the user pool

```bash
export AWS_REGION=eu-west-1
GATEWAY_URL=https://litellm.internal.example.com

POOL_ID=$(aws cognito-idp create-user-pool --pool-name gateway-idp --user-pool-tier ESSENTIALS \
  --policies '{"PasswordPolicy":{"MinimumLength":12,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
  --admin-create-user-config AllowAdminCreateUserOnly=true \
  --query UserPool.Id --output text)

# Managed login (hosted sign-in pages) on a Cognito domain prefix.
aws cognito-idp create-user-pool-domain --user-pool-id "$POOL_ID" --domain example-gateway-idp --managed-login-version 2

# The gateway URL is the resource server identifier, so it becomes the token audience.
aws cognito-idp create-resource-server --user-pool-id "$POOL_ID" --identifier "$GATEWAY_URL" \
  --name litellm-gateway --scopes ScopeName=invoke,ScopeDescription="Call the MCP gateway"

# Public client (no secret): authorization code with PKCE, short-lived access tokens.
CLIENT_ID=$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" --client-name gateway-agent-client \
  --no-generate-secret \
  --allowed-o-auth-flows code --allowed-o-auth-flows-user-pool-client \
  --allowed-o-auth-scopes openid "$GATEWAY_URL/invoke" \
  --callback-urls http://localhost:8765/callback --supported-identity-providers COGNITO \
  --explicit-auth-flows ALLOW_REFRESH_TOKEN_AUTH --prevent-user-existence-errors ENABLED --enable-token-revocation \
  --access-token-validity 60 --id-token-validity 60 --refresh-token-validity 1 \
  --token-validity-units AccessToken=minutes,IdToken=minutes,RefreshToken=days \
  --query UserPoolClient.ClientId --output text)
aws cognito-idp create-managed-login-branding --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" --use-cognito-provided-values

# One group per LiteLLM team.
aws cognito-idp create-group --user-pool-id "$POOL_ID" --group-name gw-confluence-read
aws cognito-idp create-group --user-pool-id "$POOL_ID" --group-name gw-confluence-browse

# Users: Cognito emails a temporary password; the user sets a new one at first sign-in.
aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username alice \
  --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" --username alice --group-name gw-confluence-read
```

Add `--deletion-protection ACTIVE` to the pool for anything beyond a test. Cognito group names are not immutable IDs: restrict `cognito-idp:CreateGroup`, `UpdateGroup` and `AdminAddUserToGroup` to administrators, and keep one gateway group per person ([Teams](../../README.md#teams)).

## Point the gateway at the pool

In `infra/terraform/terraform.tfvars`:

```hcl
jwt_public_key_url  = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_EXAMPLE/.well-known/jwks.json"
jwt_issuer          = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_EXAMPLE"
jwt_audience        = "https://litellm.internal.example.com"
litellm_config_path = "../../option-a-enterprise-idp/idp/cognito/config.yaml"
```

Create that `config.yaml` as a copy of [`../../proxy/config.yaml`](../../proxy/config.yaml) with two changes in `litellm_jwtauth`:

```yaml
    user_id_jwt_field: sub
    # user_email_jwt_field: email                # removed: Cognito access tokens carry no email
    user_id_upsert: true
    team_ids_jwt_field: "cognito:groups"         # quoted; the colon is part of the claim name
```

LiteLLM reads `team_ids_jwt_field` with dot-notation lookup that splits only on dots, so `cognito:groups` is read as one top-level claim (checked in the 1.109.1 source and tested: a user in `gw-confluence-read` was a member of that team, with no email). Then create the teams with the group names as team IDs, from `option-a-enterprise-idp/`:

```bash
../scripts/create_team.sh gw-confluence-read
../scripts/create_team.sh gw-confluence-browse listConfluenceSpaces listConfluenceContent atlassianUserInfo
```

On the [AgentCore runtime](../../agentcore/), set `discoveryUrl` to `https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_EXAMPLE/.well-known/openid-configuration` and `allowedAudience` to the gateway URL.

## Get a user access token

The user signs in through managed login with authorization code and PKCE, asking for the gateway as the resource (the test sent the same parameters from a script):

```bash
DOMAIN=https://example-gateway-idp.auth.eu-west-1.amazoncognito.com
VERIFIER=$(openssl rand -base64 48 | tr -d '=+/\n' | cut -c1-64)
CHALLENGE=$(printf '%s' "$VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')
STATE=$(openssl rand -hex 16)
echo "$DOMAIN/oauth2/authorize?response_type=code&client_id=$CLIENT_ID&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback&scope=openid+https%3A%2F%2Flitellm.internal.example.com%2Finvoke&code_challenge=$CHALLENGE&code_challenge_method=S256&state=$STATE&resource=https%3A%2F%2Flitellm.internal.example.com"
```

Open the URL, sign in, and copy the `code` from the address the browser lands on (`http://localhost:8765/callback?code=...&state=...`; the page itself does not load). Check that `state` matches, then exchange the code. The verifier and code go on stdin and the token to a file only the user can read:

```bash
printf 'Code: '; read -r CODE
mkdir -p ~/.config/litellm
printf 'grant_type=authorization_code&client_id=%s&code=%s&redirect_uri=http%%3A%%2F%%2Flocalhost%%3A8765%%2Fcallback&code_verifier=%s' \
  "$CLIENT_ID" "$CODE" "$VERIFIER" \
  | curl -fsS -X POST "$DOMAIN/oauth2/token" -H 'Content-Type: application/x-www-form-urlencoded' --data @- \
  | (umask 077; python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])' > ~/.config/litellm/credential)
unset CODE VERIFIER
```

The access token has `aud` set to the gateway URL, `sub`, `cognito:groups` and the `invoke` scope, and is valid for 60 minutes. Use it with the [option A helpers](../../README.md#set-up) (`GATEWAY_CREDENTIAL_FILE=~/.config/litellm/credential`) and with [`invoke.sh`](../../agentcore/invoke.sh).

## Cost and clean up

The Essentials tier is billed per monthly active user above the free tier ([Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/)). To remove the example, delete the domain, then the pool:

```bash
aws cognito-idp delete-user-pool-domain --user-pool-id "$POOL_ID" --domain example-gateway-idp
aws cognito-idp delete-user-pool --user-pool-id "$POOL_ID"
```
