# Native kernel recipe performance evidence

Status: dated historical evidence, 2026-09-13. This records a controlled experiment and the subsequently merged implementation. It is not a current execution contract or a guarantee of task latency.

The successful recipe candidate reduced managed submission-to-release time from **98.836073 to 88.682851 seconds**, an observed **10.153222 seconds (10.3%)**. Its incremental-build stage fell from **48.918923 to 40.152517 seconds**. These are individual controlled task measurements, not fresh-Agent completion times or a statistical guarantee. The [six-scenario report](six-scenario-performance-2026-09-13.md), including the slower managed ultra Agent sample and the unmet universal six-case speed objective, remains unchanged.

[Coordinator PR #29](https://github.com/vllm-ascend-workspace/vaws-coordinator/pull/29) merged at 2026-09-12 21:48:30 UTC as `3f9cdbdc631fdb3a710e08b37b29e04c3d25b85e`, after all four owner CI checks passed. Its Git tree is identical to the measured `d18a269b7c110bd32cd089257c58423ab8a5e896` candidate. The accompanying consumer pin adopts that canonical merge and retains remote-dev `4da7bbd` and all other dependency pins. The experiment itself ran the fixed candidate revision shown below.

## Scope and retained attempts

Each run made a distinct, semantically equivalent local-variable rename in the same `add_rms_norm_bias` AscendC kernel. The coordinator fixed each source snapshot, reused compatible native prerequisites, rebuilt the affected variants, and attempted the same operator validation. Source hashes therefore differ between runs. The business source baseline, image and dependencies were held fixed; remote-dev stayed at `4da7bbdd6b1a5d809d53522c9ee0b0b1d7d2e83c`.

Times are seconds from TaskClient submission to observed terminal state and resource release. Local experiment preparation, evidence retrieval and report writing are outside this boundary. The build-stage interval runs from the incremental-install progress observation to the following marker-write observation, so it includes orchestration and transitions as well as compiler work.

| Attempt | Coordinator | Submit to release | Build stage | Outcome |
| --- | --- | ---: | ---: | --- |
| Original incremental build | `91d28a9` | 98.836073 | 48.918923 | Succeeded; operator passed; resources released. |
| First recipe candidate | `358ced4` | 97.958771 | 49.886969 | Fell back to the original build because installed tiling metadata was looked up in the wrong directory; succeeded and released. This did not demonstrate a recipe-path improvement. |
| Tiling lookup corrected | `4989c2a` | 54.931356 | Not a completed stage | All three OPC variants completed, then configuration generation failed because the temporary directory omitted the expected compute-unit layout. Released; no business execution. This is not a successful latency result. |
| Layout corrected | `d18a269` | 88.682851 | 40.152517 | Recipe path completed; operator passed; resources released. |

The successful comparison saved 8.766406 seconds (17.9%) in the observed build stage. Business-process timers were 7.265429 seconds for the baseline and 7.451007 seconds for the final candidate. Those timers include imports, initialization and assertions; they are not isolated kernel latency or the complete submission-to-release interval. No repeated A/B series was run for this change.

## Work removed and correctness retained

The original incremental build still configured CMake, downloaded the third-party JSON headers and rebuilt unchanged host and tiling objects before running the three operator compiler commands. The qualified recipe path reused the verified installed prerequisites and generated the operator parameters directly. The final log contains three actual OPC completions and omits that CMake, download and host-rebuild work. The measured interval does not separately attribute the savings among those removed operations.

Eligibility retains the existing single-CPP native-input and environment compatibility checks. The recorded recipe binds effective OPC arguments, parameter hashes, generator hashes and installed output hashes. The regenerated variants must match that record before compilation. Older builds without a usable recipe, or unsupported recipe options, use the existing build path; an empty current environment is not proof of historical compiler options. This is internal preparation behavior and adds no Agent parameter or publication step.

The compiler receives the current fixed CPP through a private source copy. Guards check the actual source path and SHA256 at source lookup and compilation, and all three variants must produce complete outputs. Compilation failures propagate once compilation has started; they do not trigger a second build through fallback. The final correction preserves failed compiler evidence and keeps the upstream compute-unit directory structure.

The successful case ran FP32 `add_rms_norm_bias` with shape 16 x 128, seed 20260913 and epsilon 1e-6 against an independent CPU reference. All outputs were finite and passed rtol/atol 0.000244140625. Maximum absolute errors were 9.5367431640625e-7 for `y`, 5.960464477539063e-8 for `rstd`, and zero for `x`. Runtime evidence recorded opening the rebuilt kernel object from the execution's own output tree and its SHA256. The candidate CPP hash differed from the baseline; the opened object hash was identical, which is consistent with the semantics-preserving rename and does not replace the recorded proof of actual recompilation.

The final captured native manifest also retained the compiler recipe, allowing a subsequent eligible build to verify the same effective recipe. Local private evidence retains all four results, preparation logs, successful business results and captured manifests. This public summary omits endpoint identities and private filesystem locations.
