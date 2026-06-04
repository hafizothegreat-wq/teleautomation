/**
 * Telegram Session Manager
 * Run in browser console on https://web.telegram.org/a/
 *
 * Usage:
 *   - Export: TelegramSessionManager.exportSession()
 *   - Import: TelegramSessionManager.importSession(jsonString or File)
 */

const TelegramSessionManager = {
  /**
   * Export current Telegram session as compact JSON (localStorage only)
   */
  exportSession: function () {
    try {
      // Get localStorage data
      const localStorageData = {};
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        localStorageData[key] = localStorage.getItem(key);
      }

      // Convert to JSON string (compact format)
      const jsonString = JSON.stringify(localStorageData, null, 2);

      // Download as file
      this._downloadFile(jsonString, `telegram_session_${Date.now()}.json`);

      console.log("✅ Session exported successfully!");
      console.log("File size:", (jsonString.length / 1024).toFixed(2), "KB");
      console.log("Items:", Object.keys(localStorageData).length);
      return true;
    } catch (error) {
      console.error("❌ Export failed:", error);
      return false;
    }
  },

  /**
   * Import Telegram session from JSON file or string (localStorage only)
   */
  importSession: function (input) {
    try {
      let sessionData;

      // Handle File object
      if (input instanceof File) {
        const reader = new FileReader();
        reader.onload = (e) => {
          try {
            sessionData = JSON.parse(e.target.result);
            this._restoreLocalStorage(sessionData);
          } catch (error) {
            console.error("❌ Failed to parse file:", error);
          }
        };
        reader.readAsText(input);
        return;
      }
      // Handle string
      else if (typeof input === "string") {
        sessionData = JSON.parse(input);
      }
      // Invalid input
      else {
        throw new Error("Input must be a File or JSON string");
      }

      this._restoreLocalStorage(sessionData);
    } catch (error) {
      console.error("❌ Import failed:", error);
      return false;
    }
  },

  /**
   * Restore localStorage data
   */
  _restoreLocalStorage: function (sessionData) {
    try {
      let restored = 0;
      for (const [key, value] of Object.entries(sessionData)) {
        localStorage.setItem(key, value);
        restored++;
      }
      console.log("✅ localStorage restored:", restored, "items");
      console.log("✅ Session imported successfully!");
      console.log("🔄 Refresh the page to apply changes");
      return true;
    } catch (error) {
      console.error("❌ Failed to restore localStorage:", error);
      return false;
    }
  },

  /**
   * Download file to user's computer
   */
  _downloadFile: function (content, filename) {
    const blob = new Blob([content], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },

  /**
   * Get session info
   */
  getSessionInfo: function () {
    return {
      localStorageSize: localStorage.length,
      sessionStorageSize: sessionStorage.length,
      timestamp: new Date().toISOString(),
      url: window.location.href,
    };
  },

  /**
   * Clear all session data (use with caution!)
   */
  clearSession: function () {
    if (confirm("⚠️  This will clear all Telegram session data. Continue?")) {
      localStorage.clear();
      sessionStorage.clear();
      console.log("✅ Session cleared");
      return true;
    }
    return false;
  },
};

console.log("✅ Telegram Session Manager loaded!");
console.log("Commands:");
console.log(
  "  TelegramSessionManager.exportSession()        - Download session as compact JSON",
);
console.log(
  "  TelegramSessionManager.importSession(json)    - Import from JSON string or File",
);
console.log(
  "  TelegramSessionManager.getSessionInfo()       - Get session info",
);
console.log(
  "  TelegramSessionManager.clearSession()         - Clear all session data",
);
console.log("");
console.log("Example import:");
console.log("  TelegramSessionManager.importSession('{...json...}')");
console.log(
  '  Or use file: document.querySelector("input[type=file]").addEventListener("change", e => TelegramSessionManager.importSession(e.target.files[0]))',
);
