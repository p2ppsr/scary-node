// Build with GOOS=linux. Watch one private regtest data directory without opening its checkpoint for writing.
package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os"
	"syscall"
	"time"
)

func main() {
	if len(os.Args) != 2 {
		panic("one data directory is required")
	}
	fd, err := syscall.InotifyInit1(syscall.IN_CLOEXEC)
	if err != nil {
		panic(err)
	}
	defer syscall.Close(fd)
	mask := uint32(syscall.IN_CREATE | syscall.IN_MOVED_TO | syscall.IN_CLOSE_WRITE)
	if _, err = syscall.InotifyAddWatch(fd, os.Args[1], mask); err != nil {
		panic(err)
	}
	type event struct {
		Event   string `json:"event"`
		Name    string `json:"name"`
		Elapsed int64  `json:"elapsed_ns"`
	}
	enc := json.NewEncoder(os.Stdout)
	start := time.Now()
	if err := enc.Encode(event{Event: "ready"}); err != nil {
		panic(err)
	}
	var data [32 * 1024]byte
	for {
		n, err := syscall.Read(fd, data[:])
		if err == syscall.EINTR {
			continue
		}
		if err != nil {
			panic(err)
		}
		received := time.Since(start).Nanoseconds()
		for i := 0; i < n; {
			if n-i < 16 {
				panic("truncated inotify header")
			}
			flags := binary.NativeEndian.Uint32(data[i+4 : i+8])
			length := int(binary.NativeEndian.Uint32(data[i+12 : i+16]))
			i += 16
			if length > n-i || flags&syscall.IN_Q_OVERFLOW != 0 {
				panic("invalid or missing inotify data")
			}
			name := string(bytes.TrimRight(data[i:i+length], "\x00"))
			i += length
			if name != "witness.next" && name != "witness.state" {
				continue
			}
			var kind string
			switch flags & mask {
			case syscall.IN_CREATE:
				kind = "create"
			case syscall.IN_MOVED_TO:
				kind = "rename"
			case syscall.IN_CLOSE_WRITE:
				kind = "close_write"
			default:
				panic(fmt.Sprintf("unclassified flags %d", flags))
			}
			if err = enc.Encode(event{kind, name, received}); err != nil {
				panic(err)
			}
		}
	}
}
