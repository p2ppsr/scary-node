"""Pack exact frozen session gzip members into downloadable, independently readable shards."""

import gzip
import hashlib
import json

from cohort_archive import canonical, check, sha

SHARD_SESSIONS = 64


def shard_name(index):
    return f"sessions-{index:05d}.jsonl.gz"


def write_shards(source, output, names, atomic):
    shards = []
    for begin in range(0, len(names), SHARD_SESSIONS):
        members = []
        data = bytearray()
        for name in names[begin : begin + SHARD_SESSIONS]:
            member = (source / name).read_bytes()
            members.append({"name": name, "bytes": len(member)})
            data.extend(member)
        name = shard_name(len(shards))
        atomic(output / name, data)
        shards.append(
            {
                "file": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "members": members,
            }
        )
    atomic(
        output / "shards.json",
        canonical({"gzip_members_per_shard": SHARD_SESSIONS, "shards": shards}),
    )


def sessions(directory, expected):
    if not (directory / "shards.json").exists():
        files = {p.name for p in directory.glob("*.json.gz")}
        check(files == set(expected), "refusing incomplete or unregistered phase")
        for name in expected:
            raw = (directory / name).read_bytes()
            yield (
                name,
                json.loads(gzip.decompress(raw)),
                hashlib.sha256(raw).hexdigest(),
            )
        return
    with (directory / "shards.json").open() as file:
        index = json.load(file)
    shards = index["shards"]
    check(
        index["gzip_members_per_shard"] == SHARD_SESSIONS
        and [s["file"] for s in shards]
        == [
            shard_name(i)
            for i in range((len(expected) + SHARD_SESSIONS - 1) // SHARD_SESSIONS)
        ]
        and sorted(p.name for p in directory.glob("*.gz"))
        == [s["file"] for s in shards],
        "unexpected archive shards",
    )
    for i, s in enumerate(shards):
        path = directory / shard_name(i)
        check(
            sha(path) == s["sha256"]
            and [x["name"] for x in s["members"]]
            == expected[SHARD_SESSIONS * i : SHARD_SESSIONS * (i + 1)],
            "shard hash or registered sessions differ",
        )
        with path.open("rb") as file:
            for m in s["members"]:
                size = m["bytes"]
                check(
                    isinstance(size, int) and 0 < size <= path.stat().st_size,
                    "invalid compressed member length",
                )
                raw = file.read(size)
                check(len(raw) == size, "truncated session member")
                yield (
                    m["name"],
                    json.loads(gzip.decompress(raw)),
                    hashlib.sha256(raw).hexdigest(),
                )
            check(not file.read(1), "extra bytes beyond registered sessions")
