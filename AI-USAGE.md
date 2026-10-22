# AI usage

## Tool and scope

I used OpenAI Codex as a coding assistant to help generate implementation code for:

- The example developer application in `developer/examples/payment-service/`.
- The load-test engine in `infra/engine/`.
- Automated tests, including the engine tests in `infra/tests/`.
- Terraform code snippets and supporting scripts based on the structure, operational requirements, and security controls I supplied.

I designed the architecture, workflows, and operating procedures myself. I also defined the Terraform directory structure, module boundaries, and infrastructure organization. AI assistance was limited to implementing code and scripts within that design; these design decisions were my own.

## Representative prompts

The following are paraphrased examples of the coding requests, rather than verbatim prompt transcripts:

- "Implement the example Python payment service with health and checkout endpoints using the behavior I specify."
- "Implement the load-test engine code for configuration validation, k6 execution, result collection, and cleanup according to my workflow and constraints."
- "Write automated tests for the engine, covering configuration validation, identity checks, lifecycle handling, cleanup, and failure cases."
- "Implement the Terraform code and supporting scripts within my existing directory structure and module boundaries. Follow the operational requirements I defined for production use, including explicit environment inputs, input validation, resource limits, and cleanup. Keep this assignment's deployment in the designated non-production environment."
- "Implement my security requirements in the Terraform code: scope IAM permissions to the required resources, restrict GitHub OIDC trust to the approved repository and branch or environment, block public S3 access, enable encryption and require TLS, and keep the private EKS API enabled with any public access restricted to administrator /32 CIDRs. Keep credentials out of code and preserve my architecture."

## Agentic workflow and configuration

My use of the coding assistant followed a task-by-task process: I supplied the design and requirements, requested implementation assistance, and reviewed the resulting changes. The assistant could inspect repository files, edit code, and run local validation commands. Architecture, workflow design, Terraform organization, and operational decisions remained under my direction.

Codex also helped draft this disclosure and ran the checks listed below. No additional agent orchestration or custom agent configuration was introduced for this documentation task.

## Validation

Generated implementation needs to be checked against my requirements and the intended behavior before acceptance. AI also wrote the automated tests, so passing them is one part of validation and does not independently prove correctness. The repository includes automated checks for configuration guardrails, GitHub identity checks, lifecycle handling, cleanup, reporting, and safety monitoring.

The following checks were rerun while preparing this disclosure:

| Check | Result |
|---|---|
| `make test` | All 40 tests passed. Lifecycle tests use a simulated Kubernetes client. |
| `make validate-local` | The example payment-service configuration passed local validation. |
| `terraform -chdir=infra/live/nonprod-apse1 fmt -check` | Passed. |
| `terraform -chdir=infra/modules fmt -check -recursive` | Passed. |
| `terraform -chdir=infra/live/nonprod-apse1 validate -no-color` | Passed after rerunning outside the execution sandbox, where provider startup had failed. |

These results verify automated behavior and configuration validity. They do not establish a successful AWS deployment or an end-to-end Kubernetes load-test run; neither was performed as part of preparing this disclosure.
