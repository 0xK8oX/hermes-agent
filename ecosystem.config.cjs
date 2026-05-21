module.exports = {
  apps: [
    {
      name: "hermes-agent",
      script: "/Users/kato/.hermes/hermes-agent/venv/bin/hermes",
      args: "gateway run",
      cwd: "/Users/kato/.hermes/hermes-agent",
      log_file: "/tmp/hermes-agent-pm2.log",
      out_file: "/tmp/hermes-agent-out.log",
      error_file: "/tmp/hermes-agent-err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      min_uptime: "10s",
      watch: false,
      exec_mode: "fork",
      interpreter: "/Users/kato/.hermes/hermes-agent/venv/bin/python3",
      env: {
        PYTHONPATH: "/Users/kato/.hermes/hermes-agent",
      },
    },
  ],
};
