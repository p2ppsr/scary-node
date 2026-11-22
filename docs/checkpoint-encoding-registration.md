# Checkpoint encoding and durable writes

This comparison uses the closed height-580 reordered-workload checkpoint. Its file length is 316,588,547 bytes and the SHA-256 of its MessagePack payload is `a46c75038049c8194ce235e030bd0c515f955aa8036d88b62256232a01a72a82`. Its embedded 32-byte checksum excludes the eight-byte file magic and the checksum itself. The node executable in the workload came from source `af9b605ad8da96d041a540111d46a7c0105209f7`. A single reported native publication interval includes the return from the network call, state locking, acknowledgement, a durable save and logging. Measuring the save separately can identify time spent inside that operation.

## Paired comparison

Use Linux ARM64 on the same workstation and a separate Docker-managed volume for both writers. Take the workload measurement lock. Run the frozen release-profile serializer, with both writer variants compiled into the same benchmark executable. Open a protected copy of the source checkpoint and deserialize one `Mining` object before timing. Serialize the same object in every measurement with `rmp_serde::encode::write_named` and the original SHA-256 implementation. The existing path hashes each write from the serializer, forwarding bytes to an 8,192-byte buffered file. The comparison buffers the serializer’s bytes before hashing them as they reach the file. Keep the same checkpoint format, digest coverage, temporary file, file sync, atomic replacement and directory sync.

Run one warm-up save with each writer. Then run **24 complete pairs**, starting A then B on even-numbered pairs and B then A on odd-numbered pairs, numbered from zero. A is the existing writer. Use one directory and one destination throughout. Measure from temporary-file creation to the return of the directory sync. Keep the wall time through encoding and streamed writes, tail checksum and flush, file sync, rename and directory sync. Record hash update counts and payload bytes. Supplemental measurements time encoding to a counting sink and hashing contiguous payload bytes. These separate measurements do not decompose the end-to-end duration into additive components.

Report every timed observation, the median and range of complete paired differences and ratios, the count faster under each order, and the distribution of phase times. Capture host, VM, source, toolchain and image identities. Keep the incomplete record if a storage error, interrupted measurement, state mismatch or insufficient capacity occurs. The comparison describes this state and host. The parent workload’s propagation measurements remain separate.

## Integrity checks

Verify the archived file checksum before loading it. Outside each timed sample, compare the complete on-disk checkpoint to the writer’s reference bytes and verify the embedded hash. Reopening a Rust hash map may change its serialization order. Check the typed outputs from both variants for byte equality and compare their decoded, nested MessagePack state to the archived original. Also pass the archived payload, in its existing order, through both file writers and require byte-for-byte equality with the original file, including magic and trailing digest. Only the test fixture uses the alternative writer. [Original checkpoint implementation](https://github.com/p2ppsr/scary-node/blob/af9b605ad8da96d041a540111d46a7c0105209f7/src/mining/store.rs)

## Reproduction

From the repository root, install Python's `msgpack` package and use Docker with a Linux ARM64 backend. The build adds the ignored test module to a clean export of the registered source. The test requires a read-only copy of the closed checkpoint identified above. The output directory and Docker scratch volume must be new. The register and report contain public digests and measurements. The protected node state stays in the lab.

```sh
python3 qualification/checkpoint_profile.py build "$PROFILE_BUILD"
python3 qualification/checkpoint_profile.py run "$CHECKPOINT_COPY" "$PROFILE_BUILD" "$PROFILE_RESULTS" --scratch-volume scary-checkpoint-profile-01
python3 qualification/checkpoint_profile.py verify "$PROFILE_RESULTS/result.json.gz"
```

The run retains its raw log and the scratch volume, including the first typed reference and last timed file. The verifier recomputes the phase totals, paired differences, ratios and order counts from the retained observations. A full rerun additionally performs the byte and semantic checks against the protected checkpoint.
