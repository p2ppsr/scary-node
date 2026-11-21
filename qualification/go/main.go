package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/bitcoin-sv/bdk/module/gobdk"
	"github.com/bitcoin-sv/bdk/module/gobdk/script"
)

func main() {
	if len(os.Args) != 5 {
		panic("usage: compare extended-tx utxo-height policy-tip containing-height")
	}
	tx, err := hex.DecodeString(os.Args[1])
	if err != nil {
		panic(err)
	}
	sources := strings.Split(os.Args[2], ",")
	prev := make([]int32, len(sources))
	for i, s := range sources {
		if _, err := fmt.Sscan(s, &prev[i]); err != nil {
			panic(err)
		}
	}
	heights := make([]int32, 2)
	for i := range heights {
		if _, err := fmt.Sscan(os.Args[i+3], &heights[i]); err != nil {
			panic(err)
		}
	}
	v := script.NewTxValidator("regtest")
	if err := v.SetGenesisActivationHeight(1); err != nil {
		panic(err)
	}
	if err := v.SetChronicleActivationHeight(2); err != nil {
		panic(err)
	}
	v.SetRequireStandard(false)
	v.SetAcceptNonStandardOutput(true)
	result := map[string]any{"sv_source": gobdk.BSV_GIT_COMMIT_HASH(), "bdk_version": gobdk.BDK_VERSION_STRING(), "source": gobdk.SOURCE_GIT_COMMIT_HASH(), "sv_version": gobdk.BSV_VERSION_STRING()}
	for i, mode := range []string{"policy", "consensus"} {
		err = v.ValidateTransaction(tx, prev, heights[i], i == 1)
		result[mode] = err == nil
		if err != nil {
			result[mode+"_error"] = err.Error()
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
}
