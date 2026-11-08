const path = require('path')
module.exports = {
  version: "8.0.0",
  title: "Infinite Radio",
  description: "A never-ending, fully local AI radio station. YuE2 (via audio.cpp) composes and sings new songs back to back from a vibe you describe.",
  icon: "icon.png",
  menu: async (kernel, info) => {
    let installed = info.exists("app/env") && info.exists("app/bin") && info.exists("app/models/Yue2-3B-GGUF")
    let running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      reset: info.running("reset.js")
    }
    if (running.install) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Installing",
        href: "install.js",
      }]
    } else if (installed) {
      if (running.start) {
        let local = info.local("start.js")
        if (local && local.url) {
          return [{
            default: true,
            icon: "fa-solid fa-radio",
            text: "Open Radio",
            href: local.url,
          }, {
            icon: 'fa-solid fa-terminal',
            text: "Terminal",
            href: "start.js",
          }, {
            icon: "fa-regular fa-folder-open",
            text: "Songs",
            href: "app/output",
            fs: true
          }]
        } else {
          return [{
            default: true,
            icon: 'fa-solid fa-terminal',
            text: "Terminal",
            href: "start.js",
          }]
        }
      } else if (running.update) {
        return [{
          default: true,
          icon: 'fa-solid fa-terminal',
          text: "Updating",
          href: "update.js",
        }]
      } else if (running.reset) {
        return [{
          default: true,
          icon: 'fa-solid fa-terminal',
          text: "Resetting",
          href: "reset.js",
        }]
      } else {
        return [{
          default: true,
          icon: "fa-solid fa-power-off",
          text: "Start",
          href: "start.js",
        }, {
          icon: "fa-regular fa-folder-open",
          text: "Songs",
          href: "app/output",
          fs: true
        }, {
          icon: "fa-solid fa-plug",
          text: "Update",
          href: "update.js",
        }, {
          icon: "fa-solid fa-download",
          text: "Reinstall",
          menu: [{
            icon: "fa-solid fa-plug",
            text: "Q8 model (recommended, ~4.5 GB)",
            href: "install.js",
            params: { quant: "q8_0" }
          }, {
            icon: "fa-solid fa-plug",
            text: "Q4 model (low VRAM, ~3 GB)",
            href: "install.js",
            params: { quant: "q4_0" }
          }]
        }, {
          icon: "fa-regular fa-circle-xmark",
          text: "Reset",
          href: "reset.js",
        }]
      }
    } else {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Install (Q8 model, recommended)",
        href: "install.js",
        params: { quant: "q8_0" }
      }, {
        icon: "fa-solid fa-plug",
        text: "Install (Q4 model, low VRAM)",
        href: "install.js",
        params: { quant: "q4_0" }
      }]
    }
  }
}
