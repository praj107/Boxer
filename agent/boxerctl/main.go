// boxerctl — Boxer's optional in-guest agent.
//
// A single static binary (build with CGO_ENABLED=0) that emits the same compact
// JSON shapes as Boxer's daemon-side guest_utils fallbacks, computed in-guest in
// one round trip. Subcommands: sysinfo, services, logs, packages. The daemon
// prefers this binary when present and falls back to shell parsing otherwise, so
// the JSON contract here must match boxerd/guest_utils.py.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"syscall"
)

func main() {
	args := os.Args[1:]
	// A lone --json flag is accepted (and ignored) for call-site symmetry.
	var cmd string
	var lines = 50
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "--json":
			// no-op: boxerctl always emits JSON
		case a == "--lines" && i+1 < len(args):
			if n, err := strconv.Atoi(args[i+1]); err == nil {
				lines = n
			}
			i++
		case cmd == "":
			cmd = a
		}
	}

	switch cmd {
	case "sysinfo":
		emit(sysinfo())
	case "services":
		emit(services())
	case "logs":
		emit(logs(lines))
	case "packages":
		emit(packages())
	default:
		fmt.Fprintln(os.Stderr, "usage: boxerctl {sysinfo|services|logs|packages} [--lines N] [--json]")
		os.Exit(2)
	}
}

func emit(v interface{}) {
	b, _ := json.Marshal(v)
	os.Stdout.Write(b)
	os.Stdout.Write([]byte("\n"))
}

func run(name string, arg ...string) string {
	out, err := exec.Command(name, arg...).Output()
	if err != nil {
		return ""
	}
	return string(out)
}

func have(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

func readFile(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return string(b)
}

func nonEmptyLines(s string) []string {
	out := []string{}
	for _, l := range strings.Split(s, "\n") {
		if strings.TrimSpace(l) != "" {
			out = append(out, strings.TrimRight(l, "\r"))
		}
	}
	return out
}

// --------------------------------------------------------------------- sysinfo

func sysinfo() map[string]interface{} {
	osrel := parseOSRelease(readFile("/etc/os-release"))
	// Avoid syscall.Utsname: its array element type differs across GOARCH
	// (int8 vs uint8), which breaks cross-compilation. Read /proc instead.
	ostype := strings.TrimSpace(readFile("/proc/sys/kernel/ostype"))
	osrelease := strings.TrimSpace(readFile("/proc/sys/kernel/osrelease"))
	kernel := strings.TrimSpace(ostype + " " + osrelease)
	arch := runtime.GOARCH
	totalKB, availKB := parseMeminfo(readFile("/proc/meminfo"))
	var usedMB interface{}
	if totalKB > 0 && availKB > 0 {
		usedMB = (totalKB - availKB) / 1024
	}
	load := strings.Fields(readFile("/proc/loadavg"))
	for len(load) < 3 {
		load = append(load, "")
	}
	uptime := 0
	if f := strings.Fields(readFile("/proc/uptime")); len(f) > 0 {
		if v, err := strconv.ParseFloat(f[0], 64); err == nil {
			uptime = int(v)
		}
	}
	virt := strings.TrimSpace(run("systemd-detect-virt"))
	if virt == "" {
		virt = "unknown"
	}

	mem := map[string]interface{}{"total_mb": nil, "available_mb": nil, "used_mb": usedMB}
	if totalKB > 0 {
		mem["total_mb"] = totalKB / 1024
	}
	if availKB > 0 {
		mem["available_mb"] = availKB / 1024
	}

	return map[string]interface{}{
		"os":              osrel,
		"kernel":          orNil(strings.TrimSpace(kernel)),
		"arch":            orNil(arch),
		"virtualization":  virt,
		"cpu":             map[string]interface{}{"count": cpuCount(), "load": load[:3]},
		"memory":          mem,
		"disk_root":       diskRoot("/"),
		"uptime_seconds":  uptime,
	}
}

func parseOSRelease(s string) map[string]interface{} {
	m := map[string]string{}
	for _, l := range strings.Split(s, "\n") {
		if i := strings.Index(l, "="); i > 0 {
			k := strings.TrimSpace(l[:i])
			v := strings.Trim(strings.TrimSpace(l[i+1:]), "\"")
			m[k] = v
		}
	}
	return map[string]interface{}{
		"id":      orNil(m["ID"]),
		"name":    orNil(m["PRETTY_NAME"]),
		"version": orNil(m["VERSION_ID"]),
		"like":    orNil(m["ID_LIKE"]),
	}
}

func parseMeminfo(s string) (int, int) {
	total, avail := 0, 0
	for _, l := range strings.Split(s, "\n") {
		f := strings.Fields(l)
		if len(f) >= 2 {
			if f[0] == "MemTotal:" {
				total, _ = strconv.Atoi(f[1])
			} else if f[0] == "MemAvailable:" {
				avail, _ = strconv.Atoi(f[1])
			}
		}
	}
	return total, avail
}

func cpuCount() int {
	n := 0
	for _, l := range strings.Split(readFile("/proc/cpuinfo"), "\n") {
		if strings.HasPrefix(l, "processor") {
			n++
		}
	}
	if n == 0 {
		n = 1
	}
	return n
}

func diskRoot(path string) map[string]interface{} {
	var st syscall.Statfs_t
	if syscall.Statfs(path, &st) != nil {
		return map[string]interface{}{"mount": path}
	}
	bs := uint64(st.Bsize)
	totalKB := st.Blocks * bs / 1024
	availKB := st.Bavail * bs / 1024
	usedKB := (st.Blocks - st.Bfree) * bs / 1024
	pct := ""
	if st.Blocks > 0 {
		used := st.Blocks - st.Bfree
		denom := used + st.Bavail
		if denom > 0 {
			pct = strconv.FormatUint(used*100/denom, 10) + "%"
		}
	}
	return map[string]interface{}{
		"total_kb":     totalKB,
		"used_kb":      usedKB,
		"available_kb": availKB,
		"used_pct":     pct,
		"mount":        path,
	}
}

// -------------------------------------------------------------------- services

func services() map[string]interface{} {
	init := "unknown"
	running, failed := []string{}, []string{}
	if have("systemctl") {
		init = "systemd"
		running = unitNames(run("systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--no-pager"))
		failed = unitNames(run("systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--no-pager"))
	} else if have("rc-status") {
		init = "openrc"
		for _, l := range nonEmptyLines(run("rc-status", "-s")) {
			f := strings.Fields(l)
			if len(f) == 0 {
				continue
			}
			if strings.Contains(l, "started") {
				running = append(running, f[0])
			} else if strings.Contains(l, "crashed") || strings.Contains(l, "stopped") {
				failed = append(failed, f[0])
			}
		}
	}
	return map[string]interface{}{
		"init":            init,
		"running":         running,
		"failed":          failed,
		"listening_ports": listeningPorts(),
		"top_processes":   topProcesses(),
	}
}

func unitNames(s string) []string {
	out := []string{}
	for _, l := range nonEmptyLines(s) {
		f := strings.Fields(l)
		if len(f) > 0 {
			out = append(out, f[0])
		}
	}
	return out
}

func listeningPorts() []int {
	raw := run("ss", "-tlnH")
	if raw == "" {
		raw = run("ss", "-tln")
	}
	seen := map[int]bool{}
	ports := []int{}
	for _, l := range nonEmptyLines(raw) {
		f := strings.Fields(l)
		for _, tok := range f {
			if i := strings.LastIndex(tok, ":"); i >= 0 {
				if p, err := strconv.Atoi(tok[i+1:]); err == nil && !seen[p] {
					seen[p] = true
					ports = append(ports, p)
				}
			}
		}
	}
	return ports
}

func topProcesses() []map[string]interface{} {
	raw := run("ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu")
	out := []map[string]interface{}{}
	for i, l := range nonEmptyLines(raw) {
		if i == 0 {
			continue // header
		}
		f := strings.Fields(l)
		if len(f) >= 4 {
			pid, _ := strconv.Atoi(f[0])
			out = append(out, map[string]interface{}{
				"pid": pid, "cpu": f[1], "mem": f[2], "command": strings.Join(f[3:], " "),
			})
		}
		if len(out) >= 6 {
			break
		}
	}
	return out
}

// ------------------------------------------------------------------------ logs

func logs(n int) map[string]interface{} {
	if n < 1 {
		n = 1
	}
	failed := []string{}
	if have("systemctl") {
		failed = unitNames(run("systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--no-pager"))
	}
	var journal []string
	if have("journalctl") {
		journal = nonEmptyLines(run("journalctl", "-n", strconv.Itoa(n), "--no-pager", "-p", "warning"))
	} else if s := readFile("/var/log/messages"); s != "" {
		journal = tail(nonEmptyLines(s), n)
	} else {
		journal = tail(nonEmptyLines(readFile("/var/log/syslog")), n)
	}
	dmesg := tail(nonEmptyLines(run("dmesg", "--level=err,warn")), n)
	if len(dmesg) == 0 {
		dmesg = tail(nonEmptyLines(run("dmesg")), n)
	}
	return map[string]interface{}{"failed_units": failed, "journal": journal, "dmesg": dmesg}
}

// -------------------------------------------------------------------- packages

func packages() map[string]interface{} {
	switch {
	case have("dpkg-query"):
		all := nonEmptyLines(run("dpkg-query", "-W", "-f=${Package}\n"))
		return pkgResult("apt", all)
	case have("rpm"):
		all := nonEmptyLines(run("rpm", "-qa", "--qf", "%{NAME}\n"))
		mgr := "dnf"
		if have("zypper") && !have("dnf") {
			mgr = "zypper"
		}
		return pkgResult(mgr, all)
	case have("apk"):
		return pkgResult("apk", nonEmptyLines(run("apk", "info")))
	case have("pacman"):
		return pkgResult("pacman", nonEmptyLines(run("pacman", "-Qq")))
	}
	return map[string]interface{}{"manager": "unknown", "installed_count": 0, "sample": []string{}}
}

func pkgResult(mgr string, all []string) map[string]interface{} {
	return map[string]interface{}{"manager": mgr, "installed_count": len(all), "sample": tail(all, 50)}
}

// ----------------------------------------------------------------------- utils

func tail(s []string, n int) []string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

func orNil(s string) interface{} {
	if s == "" {
		return nil
	}
	return s
}
