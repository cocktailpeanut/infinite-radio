module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        env: {
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8"
        },
        message: [
          "python server.py",
        ],
        on: [{
          // The server prints "Infinite Radio is on the air at http://127.0.0.1:<port>"
          // once uvicorn is listening. Capture the URL and keep the shell alive.
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      }
    },
    {
      // Expose the captured URL so pinokio.js can show the "Open Radio" tab.
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
