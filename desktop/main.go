// LinguaFlow Windows companion launcher.
//
// The translation engine (offline models, LibreOffice, PDF handling) runs in
// the LinguaFlow Docker container. This tiny native executable gives Windows
// users a double-click entry point: it checks the container health endpoint,
// stores the server address next to the .exe, and opens the WebUI in the
// default browser.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const defaultServer = "http://127.0.0.1:5463"

type config struct {
	Server string `json:"server"`
}

var (
	user32   = syscall.NewLazyDLL("user32.dll")
	msgBox   = user32.NewProc("MessageBoxW")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
)

func message(title, text string, flags uint32) int {
	t, _ := syscall.UTF16PtrFromString(title)
	b, _ := syscall.UTF16PtrFromString(text)
	ret, _, _ := msgBox.Call(0, uintptr(unsafe.Pointer(b)), uintptr(unsafe.Pointer(t)), uintptr(flags))
	return int(ret)
}

func configPath() string {
	exe, err := os.Executable()
	if err != nil {
		return "linguaflow.json"
	}
	return filepath.Join(filepath.Dir(exe), "linguaflow.json")
}

func loadConfig() config {
	cfg := config{Server: defaultServer}
	raw, err := os.ReadFile(configPath())
	if err == nil && json.Unmarshal(raw, &cfg) == nil && strings.TrimSpace(cfg.Server) != "" {
		return cfg
	}
	cfg.Server = defaultServer
	_ = os.WriteFile(configPath(), []byte("{\n  \"server\": \""+cfg.Server+"\"\n}\n"), 0644)
	return cfg
}

func normalize(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return defaultServer
	}
	if !strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://") {
		raw = "http://" + raw
	}
	return strings.TrimRight(raw, "/")
}

func healthy(base string) bool {
	client := http.Client{Timeout: 4 * time.Second}
	resp, err := client.Get(base + "/api/health")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	return resp.StatusCode == 200
}

func openBrowser(url string) error {
	// ShellExecute via the URL protocol handler keeps this binary dependency-free.
	return exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
}

func main() {
	serverFlag := flag.String("server", "", "LinguaFlow server URL")
	statusOnly := flag.Bool("status", false, "only show connection status")
	flag.Parse()

	cfg := loadConfig()
	base := normalize(cfg.Server)
	if strings.TrimSpace(*serverFlag) != "" {
		base = normalize(*serverFlag)
	}
	if env := os.Getenv("LINGUAFLOW_URL"); env != "" {
		base = normalize(env)
	}

	for {
		if healthy(base) {
			if *statusOnly {
				message("LinguaFlow", "服务在线：\n"+base, 0x40) // MB_ICONINFORMATION
				return
			}
			if err := openBrowser(base + "/#/dashboard"); err != nil {
				message("LinguaFlow", "服务在线，但无法打开浏览器。\n\n请手动访问：\n"+base, 0x10) // MB_ICONERROR
			}
			return
		}
		text := fmt.Sprintf("无法连接到 LinguaFlow 服务。\n\n地址：%s\n\n请先启动 Docker 容器：\n  docker compose up -d\n\n如果服务在其他机器上，请编辑 exe 同目录的 linguaflow.json，然后点击“重试”。", base)
		if message("LinguaFlow · 连接失败", text, 0x15) != 4 { // MB_RETRYCANCEL=0x5 | MB_ICONWARNING=0x10
			return
		}
		// Pick up an address the user just edited, unless it was pinned by a
		// flag/environment variable.
		if strings.TrimSpace(*serverFlag) == "" && os.Getenv("LINGUAFLOW_URL") == "" {
			base = normalize(loadConfig().Server)
		}
	}
}
