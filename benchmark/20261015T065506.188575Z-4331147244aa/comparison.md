# NMRPeak CPU/GPU comparison: complete

Source: `4331147244aaa9cab5356b8dc864065950c3e592`

Times include materialization, tokenization, input transfer, generation, and result formatting.
Loading includes model construction, deserialization, and device transfer; filesystem caches are not cleared.
The current CPU stack and matched benchmark CPU stack are separate references.
Host/GPU activity snapshots are in the manifest; this is not a dedicated idle machine.

| Model | Case | Device | Samples | Median (s) | p95 (s) |
|---|---|---|---:|---:|---:|
| hf | small | baseline-cpu | 20 | 0.194 | 0.201 |
| hf | medium | baseline-cpu | 20 | 0.901 | 0.924 |
| hf | long | baseline-cpu | 20 | 2.910 | 3.007 |
| chf | small | baseline-cpu | 20 | 0.243 | 0.252 |
| chf | medium | baseline-cpu | 20 | 1.013 | 1.054 |
| chf | long | baseline-cpu | 20 | 3.509 | 3.616 |
| hf | small | cpu | 20 | 0.196 | 0.201 |
| hf | medium | cpu | 20 | 0.907 | 0.933 |
| hf | long | cpu | 20 | 2.972 | 3.051 |
| chf | small | cpu | 20 | 0.252 | 0.259 |
| chf | medium | cpu | 20 | 1.038 | 1.058 |
| chf | long | cpu | 20 | 3.600 | 3.656 |
| hf | small | GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 | 20 | 0.050 | 0.051 |
| hf | medium | GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 | 20 | 0.149 | 0.151 |
| hf | long | GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 | 20 | 0.344 | 0.348 |
| chf | small | GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 | 20 | 0.060 | 0.061 |
| chf | medium | GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 | 20 | 0.157 | 0.158 |
| chf | long | GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 | 20 | 0.395 | 0.397 |
| hf | small | GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 | 20 | 0.049 | 0.050 |
| hf | medium | GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 | 20 | 0.170 | 0.171 |
| hf | long | GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 | 20 | 0.503 | 0.504 |
| chf | small | GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 | 20 | 0.059 | 0.060 |
| chf | medium | GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 | 20 | 0.183 | 0.185 |
| chf | long | GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 | 20 | 0.603 | 0.605 |

## Speed and output agreement

- hf/small, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against cpu: 3.94×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/small, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against baseline-cpu: 3.89×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/medium, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against cpu: 6.09×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/medium, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against baseline-cpu: 6.05×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/long, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against cpu: 8.63×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/long, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against baseline-cpu: 8.45×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/small, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against cpu: 4.22×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/small, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against baseline-cpu: 4.06×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/medium, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against cpu: 6.63×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/medium, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against baseline-cpu: 6.47×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/long, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against cpu: 9.13×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/long, GPU-75078110-7bdd-beaa-2195-2ba33a555cd7 against baseline-cpu: 8.89×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/small, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against cpu: 4.01×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/small, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against baseline-cpu: 3.95×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/medium, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against cpu: 5.34×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/medium, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against baseline-cpu: 5.31×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/long, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against cpu: 5.91×; ranked equality 20/20; mean molecule overlap 1.000.
- hf/long, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against baseline-cpu: 5.79×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/small, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against cpu: 4.30×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/small, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against baseline-cpu: 4.14×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/medium, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against cpu: 5.66×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/medium, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against baseline-cpu: 5.52×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/long, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against cpu: 5.97×; ranked equality 20/20; mean molecule overlap 1.000.
- chf/long, GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347 against baseline-cpu: 5.82×; ranked equality 20/20; mean molecule overlap 1.000.

## Startup, inputs, memory, and candidate validity

- hf-baseline-cpu: verification_seconds=0.522s; model_load_and_transfer_seconds=5.675s; elapsed_seconds=0.197s
- chf-baseline-cpu: verification_seconds=0.518s; model_load_and_transfer_seconds=5.558s; elapsed_seconds=0.227s
- hf-cpu: verification_seconds=0.517s; model_load_and_transfer_seconds=5.441s; elapsed_seconds=0.200s
- chf-cpu: verification_seconds=0.521s; model_load_and_transfer_seconds=5.432s; elapsed_seconds=0.249s
- hf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7: verification_seconds=0.513s; model_load_and_transfer_seconds=5.442s; elapsed_seconds=0.163s
- chf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7: verification_seconds=0.510s; model_load_and_transfer_seconds=5.489s; elapsed_seconds=0.176s
- hf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347: verification_seconds=0.523s; model_load_and_transfer_seconds=5.417s; elapsed_seconds=0.156s
- chf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347: verification_seconds=0.517s; model_load_and_transfer_seconds=5.498s; elapsed_seconds=0.134s
- hf-baseline-cpu/small: 15 input tokens; process peak RSS 2004.4 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 180/200; candidate character lengths [2, 3, 5, 6, 7]; first actual generation 0.197s.
- hf-baseline-cpu/medium: 37 input tokens; process peak RSS 2004.4 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 160/200; candidate character lengths [18, 20, 21, 22, 23].
- hf-baseline-cpu/long: 91 input tokens; process peak RSS 2004.4 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 200/200; candidate character lengths [45, 46, 47, 48, 49].
- chf-baseline-cpu/small: 18 input tokens; process peak RSS 2004.3 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 120/200; candidate character lengths [6, 7, 8, 9]; first actual generation 0.227s.
- chf-baseline-cpu/medium: 48 input tokens; process peak RSS 2004.3 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 180/200; candidate character lengths [22, 23, 24, 25].
- chf-baseline-cpu/long: 116 input tokens; process peak RSS 2004.3 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 200/200; candidate character lengths [52, 53, 54].
- hf-cpu/small: 15 input tokens; process peak RSS 2388.0 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 180/200; candidate character lengths [2, 3, 5, 6, 7]; first actual generation 0.200s.
- hf-cpu/medium: 37 input tokens; process peak RSS 2388.0 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 160/200; candidate character lengths [18, 20, 21, 22, 23].
- hf-cpu/long: 91 input tokens; process peak RSS 2388.0 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 200/200; candidate character lengths [45, 46, 47, 48, 49].
- chf-cpu/small: 18 input tokens; process peak RSS 2388.3 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 120/200; candidate character lengths [6, 7, 8, 9]; first actual generation 0.249s.
- chf-cpu/medium: 48 input tokens; process peak RSS 2388.3 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 180/200; candidate character lengths [22, 23, 24, 25].
- chf-cpu/long: 116 input tokens; process peak RSS 2388.3 MiB; GPU peak allocated/reserved 0.0/0.0 MiB; valid candidates 200/200; candidate character lengths [52, 53, 54].
- hf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7/small: 15 input tokens; process peak RSS 2705.2 MiB; GPU peak allocated/reserved 428.6/506.0 MiB; valid candidates 180/200; candidate character lengths [2, 3, 5, 6, 7]; first actual generation 0.163s.
- hf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7/medium: 37 input tokens; process peak RSS 2705.2 MiB; GPU peak allocated/reserved 440.0/506.0 MiB; valid candidates 160/200; candidate character lengths [18, 20, 21, 22, 23].
- hf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7/long: 91 input tokens; process peak RSS 2705.2 MiB; GPU peak allocated/reserved 454.5/506.0 MiB; valid candidates 200/200; candidate character lengths [45, 46, 47, 48, 49].
- chf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7/small: 18 input tokens; process peak RSS 2705.3 MiB; GPU peak allocated/reserved 429.9/506.0 MiB; valid candidates 120/200; candidate character lengths [6, 7, 8, 9]; first actual generation 0.176s.
- chf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7/medium: 48 input tokens; process peak RSS 2705.3 MiB; GPU peak allocated/reserved 440.4/506.0 MiB; valid candidates 180/200; candidate character lengths [22, 23, 24, 25].
- chf-GPU-75078110-7bdd-beaa-2195-2ba33a555cd7/long: 116 input tokens; process peak RSS 2705.3 MiB; GPU peak allocated/reserved 459.8/506.0 MiB; valid candidates 200/200; candidate character lengths [52, 53, 54].
- hf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347/small: 15 input tokens; process peak RSS 2616.1 MiB; GPU peak allocated/reserved 428.6/506.0 MiB; valid candidates 180/200; candidate character lengths [2, 3, 5, 6, 7]; first actual generation 0.156s.
- hf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347/medium: 37 input tokens; process peak RSS 2616.1 MiB; GPU peak allocated/reserved 440.0/506.0 MiB; valid candidates 160/200; candidate character lengths [18, 20, 21, 22, 23].
- hf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347/long: 91 input tokens; process peak RSS 2616.1 MiB; GPU peak allocated/reserved 454.5/506.0 MiB; valid candidates 200/200; candidate character lengths [45, 46, 47, 48, 49].
- chf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347/small: 18 input tokens; process peak RSS 2614.9 MiB; GPU peak allocated/reserved 429.9/506.0 MiB; valid candidates 120/200; candidate character lengths [6, 7, 8, 9]; first actual generation 0.134s.
- chf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347/medium: 48 input tokens; process peak RSS 2614.9 MiB; GPU peak allocated/reserved 440.4/506.0 MiB; valid candidates 180/200; candidate character lengths [22, 23, 24, 25].
- chf-GPU-45b76e53-80bd-70cb-dd21-857e4b7f5347/long: 116 input tokens; process peak RSS 2614.9 MiB; GPU peak allocated/reserved 459.8/506.0 MiB; valid candidates 200/200; candidate character lengths [52, 53, 54].
