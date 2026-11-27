# Option A agent on Amazon Bedrock AgentCore Runtime

This folder deploys the option A agent to [AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html) as an ARM64 container. The runtime accepts only access tokens from your IdP (inbound JWT authorizer), passes the token to the agent through the `Authorization` header allowlist, and the agent sends it to the gateway on every model and MCP call. The runtime role has no Amazon Bedrock permissions: every model call goes through the gateway.

Tested in November 2026 with LiteLLM proxy 1.109.1 and the pins in [`requirements.txt`](requirements.txt), with Amazon Cognito as the IdP ([worked example](../idp/cognito/)).

| File | Purpose |
|---|---|
| [`agent.py`](agent.py) | AgentCore entrypoint; `ask()` is the same as in [`../client/agent.py`](../client/agent.py) |
| [`Dockerfile`](Dockerfile) | ARM64 image, non-root user, private CA appended to the certifi bundle |
| [`create-runtime.json.example`](create-runtime.json.example) | Input for `create-agent-runtime`: VPC mode, JWT authorizer, header allowlist, environment |
| [`trust-policy.json`](trust-policy.json), [`exec-policy.json`](exec-policy.json) | Runtime execution role ([AgentCore Runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)) |
| [`invoke.sh`](invoke.sh) | Calls the runtime as a user with a bearer token |

All IDs in the examples are fictitious (account `111122223333`). Replace the account, Region, subnets, security groups, IdP discovery URL and gateway URL with yours.

## Deploy

Run from this folder, after the gateway is deployed and healthy ([infrastructure](../../infra/terraform/README.md#deploy)). In zsh, write `${ACCOUNT}:` with braces in ARNs: zsh reads `$ACCOUNT:r` and similar as history modifiers.

1. **Private CA.** Copy the CA chain that issued the gateway certificate to `private-ca.pem` in this folder. The image appends it to the public roots and the runtime points `SSL_CERT_FILE` at the result.
2. **Build and push the image.**
   ```bash
   export AWS_REGION=eu-west-1 ACCOUNT=111122223333
   REPO_URI=$(aws ecr create-repository --repository-name confluence-agent \
     --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
     --encryption-configuration encryptionType=KMS --query repository.repositoryUri --output text)
   aws ecr get-login-password | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
   docker build --platform linux/arm64 -t "${REPO_URI}:1" .
   docker push "${REPO_URI}:1"
   ```
3. **Execution role.** Edit the account and Region in both policy files first.
   ```bash
   aws iam create-role --role-name confluence-agent-runtime --assume-role-policy-document file://trust-policy.json
   aws iam put-role-policy --role-name confluence-agent-runtime --policy-name runtime --policy-document file://exec-policy.json
   ```
4. **Network.** Use private subnets in [Availability Zones that AgentCore supports for VPC mode](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html), with a route to the gateway load balancer and outbound HTTPS (Amazon ECR, Amazon S3 and CloudWatch Logs, through the NAT gateway or VPC endpoints). Attach two security groups: `gateway_clients_security_group_id` from the Terraform outputs (HTTPS to the load balancer), and one with no inbound rules and outbound TCP 443 only:
   ```bash
   VPC_ID=vpc-0example1
   AGENT_SG=$(aws ec2 create-security-group --group-name confluence-agent-egress \
     --description "AgentCore agent: outbound HTTPS only" --vpc-id "$VPC_ID" --query GroupId --output text)
   aws ec2 revoke-security-group-egress --group-id "$AGENT_SG" --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
   aws ec2 authorize-security-group-egress --group-id "$AGENT_SG" --ip-permissions '[{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
   ```
5. **Create the runtime.** Copy `create-runtime.json.example` to `create-runtime.json` and set the image URI, role ARN, subnets, both security groups, the IdP discovery URL (`<issuer>/.well-known/openid-configuration`), the audience and the gateway URL. The audience must be accepted by both the runtime authorizer and LiteLLM (`JWT_AUDIENCE`).
   ```bash
   RUNTIME_ID=$(aws bedrock-agentcore-control create-agent-runtime --cli-input-json file://create-runtime.json \
     --query agentRuntimeId --output text)
   aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" --query status --output text   # repeat until READY
   export RUNTIME_ARN=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
     --query agentRuntimeArn --output text)
   ```
   In our test, the runtime went from `CREATING` to `READY` in about 3 minutes.
6. **Invoke as a user.** Save the user's access token (without `Bearer `) in a file only the user can read, for example `~/.config/litellm/credential` as in the [option A set-up](../README.md#set-up), then:
   ```bash
   ./invoke.sh ~/.config/litellm/credential "List the Confluence spaces I can see."
   ```
   Before the user has connected Atlassian, the agent answers that it has no Confluence tools; afterwards it lists the spaces that user can see. A request with no token or a malformed token returns HTTP 401, and a SigV4-signed call (`aws bedrock-agentcore invoke-agent-runtime`) returns `AccessDeniedException: Authorization method mismatch`.

## Notes

- **Token handling.** `agent.py` uses the caller's token for that request only. Never cache it across users, and never fall back to a shared credential.
- **Invocation endpoint.** `invoke.sh` calls the Regional AgentCore endpoint `bedrock-agentcore.<region>.amazonaws.com`, a public AWS service endpoint that only accepts valid IdP tokens for this runtime. To keep invocation traffic inside your network, call it through an [interface VPC endpoint for AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html) (`com.amazonaws.<region>.bedrock-agentcore`). Bearer-token calls through the endpoint need `"Principal": "*"` in the endpoint policy, because endpoint policies match IAM principals, not OAuth users.
- **Image findings.** An Amazon ECR basic scan of this image found no critical and several high and medium findings, all in Debian base packages. See the comment in the [`Dockerfile`](Dockerfile) and use Amazon Inspector enhanced scanning in production.
- **Clean up.** Delete the runtime first (`aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$RUNTIME_ID"`): its network interfaces can stay for up to 8 hours and block deleting the subnets or security groups. Then delete the security group, the role and its policy, and the ECR repository.
