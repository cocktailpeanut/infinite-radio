// Removes everything install.js created. Generated songs in app/output and the
// library index in app/data are kept.
module.exports = {
  run: [{
    method: "fs.rm",
    params: {
      path: "app/env"
    }
  }, {
    method: "fs.rm",
    params: {
      path: "app/bin"
    }
  }, {
    method: "fs.rm",
    params: {
      path: "app/models"
    }
  }, {
    method: "fs.rm",
    params: {
      path: "app/runtime.json"
    }
  }]
}
