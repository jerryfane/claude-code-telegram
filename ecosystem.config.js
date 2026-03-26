module.exports = {
  apps: [
    {
      name: "claude-telegram",
      script: "/home/pi/.cache/pypoetry/virtualenvs/claude-code-telegram-Gi-D3A84-py3.13/bin/claude-telegram-bot",
      interpreter: "/home/pi/.cache/pypoetry/virtualenvs/claude-code-telegram-Gi-D3A84-py3.13/bin/python",
      cwd: "/home/pi/claude-code-telegram",
      env: {
        PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
        PATH: "/home/pi/.local/bin:/usr/local/bin:/usr/bin:/bin",
        CLAUDECODE: "",
      },
      filter_env: ["CLAUDECODE"],
      kill_timeout: 10000,
      restart_delay: 5000,
      max_restarts: 10,
      autorestart: true,
    },
    {
      name: "claude-dashboard",
      script: "npx",
      args: "vite --host 0.0.0.0",
      cwd: "/home/pi/claude-code-telegram/website/frontend",
      autorestart: true,
    },
  ],
}
