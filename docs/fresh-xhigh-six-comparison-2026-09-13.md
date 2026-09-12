# Fresh xhigh scenario-6 comparison

Status: dated historical evidence, 2026-09-13. Both arms completed business execution, necessary evidence verification and their reports. This supplements the earlier experiments; it does not replace their failed or slower samples or establish a current execution contract.

In this fresh GPT-6 Astra / xhigh comparison, managed first-tool-to-business-completion time was **243.363 seconds**, versus **470.210 seconds** for direct. Including necessary evidence verification, the totals were **446.631 versus 585.052 seconds**; including each arm's report, **562.546 versus 714.009 seconds**. The observed reductions were **48.2%, 23.7% and 21.2%**, respectively. This is one matched-business-input observation with the setup and compiler-option differences below, not a universal performance guarantee. The [six-scenario report](six-scenario-performance-2026-09-13.md) and [native-recipe controlled experiment](native-kernel-recipe-performance-2026-09-13.md) retain their original results.

## Scope

Both new Agents were separately dispatched as GPT-6 Astra with xhigh reasoning and no inherited conversation. Neither used a Skill or earlier experiment driver/report. The direct Agent used ordinary local shell/files and SSH. The managed Agent used the public TaskClient/CLI through one fixed coordinator interpreter and public entry documentation. These are single autonomous observations, not repeated randomized A/B measurements.

Each started with a different existing container, no owned source/build view, no donor bind mounts and the same fixed image and business dependency versions. Both could reuse compatible existing native prerequisites. The direct Agent copied an existing donor into its own private view inside its timer. Managed received an ordinary local source worktree and explicit native attachment; remote source preparation, native reuse, compilation and execution remained inside its timer. The local source copy/attachment are setup differences, stated separately below. Neither arm updated its image or dependencies. Physical-device business work was sequential between arms.

The common business shell was created before both timers and supplied to both Agents as a task input. Reading, transferring and executing it belongs to each Agent's timer. It compiles a small native open-audit helper and runs one FP32 `npu_add_rms_norm_bias` case with shape 16 x 128, seed 20260913, epsilon 1e-6 and independent CPU-reference checks at rtol/atol 0.000244140625. It also checks owned module origins, the requested CPP marker and actual owned kernel-object opening. This common-input experiment differs from earlier trials in which Agents independently wrote their business tests.

Both were asked to rename only the CPP macro-local variable `op` and its two uses to `fresh_xhigh_op`, preserving LF, and compile all three generated dtype variants. A semantics-preserving rename can yield identical object hashes; actual successful compiler invocations, fresh output provenance and source evidence establish recompilation together.

The managed environment was frozen at coordinator `d18a269b7c110bd32cd089257c58423ab8a5e896` and remote-dev `4da7bbdd6b1a5d809d53522c9ee0b0b1d7d2e83c`. Its coordinator tree equals the canonical PR #29 merge `3f9cdbdc631fdb3a710e08b37b29e04c3d25b85e`. Editable package version strings alone do not establish that revision; the frozen source HEADs were checked. The direct arm used neither component.

## Comparable endpoints

All end-to-end durations use local UTC endpoints. Remote clocks differ and are not subtracted from local timestamps. Failed attempts, exploration, script construction, retries and tool gaps remain included. An internal business timer omits complete interpreter teardown and is not the headline Agent duration.

| Boundary | Direct seconds | Managed seconds |
| --- | ---: | ---: |
| First actual tool to full business process exit / observed quiet and released | 470.2097974 | 243.3633692 |
| First actual tool to necessary evidence retrieved and verified | 585.0516937 | 446.6310842 |
| First actual tool to arm report written | 714.0092981 | 562.5463242 |

Direct first tool was `2026-09-12T21:42:42.4829687Z`, full business SSH exit `21:50:32.6927661Z`, evidence verified `21:52:27.5346624Z`, and report completed `21:54:36.4922668Z`. The managed first tool was `2026-09-12T21:55:47.8467488Z` and successful quiet/released observation was `21:59:51.210118Z`.

Managed submitted at `21:57:54.206972Z`, 126.360223 seconds after its first tool. Initial cold submission returned queued after 6.627 seconds. Submission to release was 117.003146 seconds; this is a new, slower submission-to-release observation than the earlier controlled 88.682851-second recipe run, not a replacement for it. Independent discovery/script preparation plus managed execution together completed 226.846428 seconds (48.2%) sooner than this direct Agent. The single observation does not isolate model variability, entry documentation, compiler options and runtime optimization as individual causes.

Managed necessary evidence was verified at `22:03:14.477833Z` and its report finished at `22:05:10.393073Z`. Including necessary evidence, managed finished 138.420610 seconds (23.7%) sooner. Its 203.267715 seconds after release through evidence completion was longer than direct's 114.841896 seconds. This interval includes Agent reasoning, discovery, local verification and bounded read-only evidence retrieval; it is not a single remote RPC duration or compiler cost. It is retained in the comparison rather than hidden after the business endpoint. Benchmark provenance verification should not become a new mandatory daily-task workflow.

## Managed outcome

The managed Agent edited its private local CPP and submitted one source-bound execution. The coordinator automatically restored a matching shared-native bundle and performed the three-variant incremental recipe; the Agent issued no manual cache-publication or custom compiler command. The common business passed with an internal 7.343931-second timer, finite outputs and the same small reference errors as direct. Its actual source SHA256 equals direct's edited CPP byte for byte, and the runtime-opened FP32 object SHA256 equals the captured owned output.

Owner evidence recorded `shared-native` reuse, all three successful OPC variants and complete outputs. Retrieved recipe and output hashes matched the captured manifest, and the targeted dispatch metadata mapped FP16/FP32/BF16 to those outputs. The execution was succeeded, quiet and resources released. The Agent performed no additional NPU business run while collecting evidence.

There were no managed runtime failures, failed compiler batches or business reruns. Local evidence discovery retried a no-match search for the compressed manifest and an ignored-path inventory; verbose tool output was also truncated, while complete records remained on disk. Two targeted SSH reads retrieved only the owned reuse/recipe and dispatch metadata after release. Those calls and local retries are included in the evidence endpoint. The manifest itself came from the existing owner log rather than another remote audit. Sparse progress observations do not isolate every stage; the 117.003-second submission interval includes the Agent's observation delay and should not be described as pure backend execution time.

## Direct outcome and retained failures

Direct copied 718 MB privately and attempted two three-variant compiler batches. An early device diagnostic lacked the driver-library environment. Default SCP failed because SFTP was absent and succeeded using legacy SCP. The first compiler batch lacked custom tiling-library settings; the Agent inspected the actual upstream CMake files, repaired its private environment and reran all variants into new empty output directories. None of those failures is removed from its timer.

The successful float16/float32/bfloat16 compiler intervals were 38.021333 / 38.534278 / 38.679504 seconds and overlapped. Direct additionally chose OPC debug logging and preprocessor-output flags; their overhead was not isolated, so differences from the owner's default compiler interval cannot be attributed wholly to orchestration. The unchanged common business input ran once and passed on its first invocation; its internal timer was 7.942281 seconds, beginning before framework imports. Its complete remote business shell process took 11.670384 seconds. The final CPP SHA256 was `807721f07f5922bddc8b48f7508a2a86ed467143735eec593238ed6a550a7b2d`. The process opened its owned FP32 object whose SHA256 matched the newly compiled output. Failed and successful compiler evidence remain retained locally.

## Setup boundary and limits

Existing-container setup was outside both Agent timers: 36.949420 seconds for direct and 36.813730 seconds for managed. Neither container had donor bind mounts or an owned prepared source/build view. The managed ordinary local source-copy/attachment preparation had a Windows long-path failure followed by recovery; its two local preparation processes totalled 6.949925 seconds outside the Agent timer. That sum is process time, not a claim to include all experiment-owner planning and intervention. The source copy contained only tracked sources/submodules and a `.gitkeep` native-package placeholder, with no compiled artifacts or prepublished source snapshots. These setup facts must remain visible when interpreting the comparison.

The experiment covers one single-CPP edit and one numerical input, not every operator, every model, every source state or all six scenarios. The historical case-1/3 warm managed tool interval remains longer than a direct successful SSH command, and the earlier context-exposed ultra scenario-6 managed Agent was slower. Any fresh result here is additional evidence about total Agent goal cost, not permission to drop those observations or declare a universal speed guarantee.
