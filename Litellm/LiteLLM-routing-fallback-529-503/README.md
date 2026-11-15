# Handle Amazon Bedrock 529 and 503 errors with LiteLLM

![LiteLLM on Amazon Bedrock architecture](./images/litellm-bedrock-architecture.svg)

Code for the post "Handle Anthropic overload errors on Amazon Bedrock with LiteLLM". It covers:

- The LiteLLM routing and fallback config from the post (`proxy/litellm-config.yaml`).
- Tests that show how LiteLLM 1.109.1 handles Bedrock 529 and 503 errors, and how it retries and falls back.
- Measurements of where Claude Opus 5.5 and Opus 5 requests are served, how long long generations take, and how prompts are cached.

## What the measurements showed

These results come from one AWS account, entry Regions us-east-1 and eu-west-1, in November 2026. Placement can change over time, so measure your own account before you rely on any of these numbers.

- **Placement is per model.** Claude Opus 5.5 (`us.` profile) was served from us-east-1 in 100% of calls. Nova Lite and Nova Micro spread across three US Regions. The EU Opus 5.5 profile stayed on eu-north-1 for a 24-hour run.
- **The entry Region doesn't change the serving Region.** Calls to `eu.anthropic.claude-opus-5-5` entered from eu-west-1 and from eu-north-1 were both served from eu-north-1.
- **A global profile can serve from another continent.** `global.anthropic.claude-opus-5-5` called from eu-north-1 was served from ap-southeast-4.
- **Prompt caches are per Region.** A 5,543-token prefix written in one serving Region was not reused from another. Each new Region paid a full cache write.
- **Long generations exceed 60 seconds.** Opus 5.5 produced 4,096 output tokens in a median of 43 s (p99 46 s, 30 calls). One 10,000-token call took 110 s. Set the gateway `timeout` above your own p99, not the 60 s boto3 default.
- **Retries multiply load during an outage.** With the blog config and every call returning 529 or 503, one request made 18 Bedrock attempts over about 52 s.

Caveats:

- The 529 and 503 tests use mocked Bedrock responses. We did not capture a real Bedrock overload response, so we can't confirm which status code Bedrock returns during an Anthropic overload.
- Latency was measured for non-streaming calls only.
- We did not observe any cache-aware placement. The results show only that caches are per Region.

## Repository layout

```
proxy/
  litellm-config.yaml          # model_list and router_settings from the post
sdk/
  router_fallback_mock_test.py      # mocked 529/503: exception types and retry/fallback counts (no AWS calls)
  router_fallback_live_test.py      # live Router test: each alias, forced fallback, timeout, invocation-log check
  cris-routing-by-model.py          # serving Region per model, 30 calls each
  cris-pinning-over-time.py         # serving Region over time (24 h, 15-minute rounds)
  cris-pinning-20261108-2340.csv    # output of the 24-hour run used in the post
  litellm-sdk-cris-prompt-cache-test.py     # cache write/read across Regions
  opus-long-generation-latency.py           # time to last token on long generations
  opus-long-generation-latency-20261114-2225.csv  # output of the latency run used in the post
  enable_bedrock_invocation_logging.py      # enables invocation logging in each Region (needed by the log-based tests)
```

## Prerequisites

- Python 3.10 or later, with `pip install "litellm==1.109.1" boto3 aws-bedrock-token-generator pyyaml`. The tests were run on LiteLLM 1.109.1.
- An AWS account with access to Claude Opus 5.5 and Opus 5 in Amazon Bedrock. Confirm the inference profile IDs for your account with `aws bedrock list-inference-profiles --region <region>`.
- AWS credentials in your shell, for example from `aws login` or a named profile. The scripts never read static keys from the code.
- An IAM identity allowed to call `bedrock:InvokeModel` on the profiles you use. The log-based tests also need `logs:FilterLogEvents` on the log group, and `cloudtrail:LookupEvents` if you check CloudTrail.
- Model invocation logging enabled to CloudWatch Logs in each Region you call from. Run `sdk/enable_bedrock_invocation_logging.py` to set it up. It creates an S3 bucket, a log group and an IAM role in each Region.
- An existing LiteLLM configuration with Amazon Bedrock running in your environment.

## Configuration

`proxy/litellm-config.yaml` uses the placeholder account `111111111111` in its `aws_role_name` values. Do either of these:

- Set `LITELLM_ROLE_ARN` to a role your identity can assume. The live test uses it for every deployment.
- Leave it unset. The live test drops `aws_role_name` and uses your calling credentials.

To run the gateway with the config:

```bash
pip install "litellm[proxy]==1.109.1"
```

Replace the account in `aws_role_name` first, or remove those lines.

## Running the tests

Run these from the `sdk/` folder.

| Script | Needs AWS | Time | What it shows |
|---|---|---|---|
| `router_fallback_mock_test.py` | No | About 2 min | Exception type for 529 and 503, and the number of attempts across the three aliases. |
| `router_fallback_live_test.py` | Yes | About 3 min | One request per alias, forced fallbacks, a 5-second timeout, and the invocation-log records. |
| `cris-routing-by-model.py` | Yes | About 5 min | Serving Region for six models, 30 calls each. |
| `cris-pinning-over-time.py --hours 24 --interval 15` | Yes | 24 h | Serving Region over time. Use `--hours 0.5 --interval 2` for a short check. Use `--report FILE.csv` to print a summary from a saved run. |
| `litellm-sdk-cris-prompt-cache-test.py` | Yes | About 5 min | Cache writes and reads for the same prefix across geographies. |
| `opus-long-generation-latency.py --n 30 --workers 5` | Yes | About 5 min | Time to last token and percentiles for 2,000+ token outputs. |

The log-based scripts read the serving Region from invocation logs. Logs appear about one minute after a call, and the scripts retry until they do.

Costs are small: each run makes at most a few hundred calls with short prompts. The latency test generates 4,096 tokens per call, so it costs the most.

## Key facts about the APIs

- **`inferenceRegion` is the serving Region.** It appears in the Bedrock invocation log and in CloudTrail (`additionalEventData.inferenceRegion`). The top-level `region` and `awsRegion` fields hold the entry Region.
- **Invocation logs are written to the entry Region's log group only.** To find a call's serving Region, query the log group in the Region you called from.
- **`requestMetadata` is the tag field.** LiteLLM passes it to Bedrock, and it appears in the invocation log, not in CloudTrail management events.
- **Use the profile ID without a version suffix,** for example `eu.anthropic.claude-opus-5-5`. A `-v1:0` suffix returns `ResourceNotFoundException`.
- **A geo profile must be called from a Region in its geography.** Calling `eu.` from us-east-1 returns "The provided model identifier is invalid."

## Security

- The scripts use the AWS credentials in your environment. Don't commit keys, bearer tokens or `.env` files.
- The invocation logs contain prompts and responses. The setup script keeps them for 90 days by default. Change `LOG_RETENTION_DAYS` and `S3_EXPIRY_DAYS` to match your policy.
- The mock and latency scripts send only generic prompts. Don't reuse them with production data without reviewing where the logs go.
- The gateway should not be exposed without authentication. See the LiteLLM documentation on `master_key` and virtual keys.

## Notes

- The routing config sends requests to a `global.` profile, which can serve from any Bedrock Region. Check the data-residency rules for your workload before you use it.
- The fallback model, Claude Opus 5, is a different model from the primary. Confirm that your prompts and output format work on it before you rely on it.
