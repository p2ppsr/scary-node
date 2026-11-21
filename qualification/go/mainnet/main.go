// Replay private extended transactions on stdin using unmodified mainnet GoBDK defaults.
package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/bitcoin-sv/bdk/module/gobdk"
	"github.com/bitcoin-sv/bdk/module/gobdk/script"
)

type input struct {
	Extended string  `json:"extended"`
	Heights  []int32 `json:"heights"`
	Parent   int32   `json:"parent"`
}

func run(in io.Reader, out io.Writer) error {
	v := script.NewTxValidator("main")
	if v == nil {
		return fmt.Errorf("cannot initialize mainnet interpreter")
	}
	decoder, encoder := json.NewDecoder(in), json.NewEncoder(out)
	if err := encoder.Encode(map[string]string{
		"network": "main", "source": gobdk.SOURCE_GIT_COMMIT_HASH(),
		"sv_source": gobdk.BSV_GIT_COMMIT_HASH(), "bdk_version": gobdk.BDK_VERSION_STRING(),
		"sv_version": gobdk.BSV_VERSION_STRING(),
	}); err != nil {
		return err
	}
	for {
		var row input
		if err := decoder.Decode(&row); err != nil {
			if err == io.EOF {
				return nil
			}
			return err
		}
		tx, err := hex.DecodeString(row.Extended)
		if err != nil {
			return err
		}
		policy := v.ValidateTransaction(tx, row.Heights, row.Parent, false)
		consensus := v.ValidateTransaction(tx, row.Heights, row.Parent+1, true)
		reply := map[string]any{"policy": policy == nil, "consensus": consensus == nil}
		if policy != nil {
			reply["policy_error"] = policy.Error()
		}
		if consensus != nil {
			reply["consensus_error"] = consensus.Error()
		}
		if err := encoder.Encode(reply); err != nil {
			return err
		}
	}
}

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
