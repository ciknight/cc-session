(() => {
  const status = document.getElementById("copy-status");
  const chinese = document.documentElement.lang.toLowerCase().startsWith("zh");

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }

    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    if (!copied) throw new Error("Copy command was rejected");
  }

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const original = button.textContent;
      window.clearTimeout(button.copyResetTimer);
      try {
        await copyText(button.dataset.copy);
        button.textContent = chinese ? "已复制" : "Copied";
        button.classList.add("copied");
        status.textContent = chinese ? "命令已复制到剪贴板。" : "Command copied to clipboard.";
      } catch {
        button.textContent = chinese ? "复制失败" : "Copy failed";
        status.textContent = chinese ? "复制失败，请手动选择命令。" : "Copy failed. Select the command manually.";
      }
      button.copyResetTimer = window.setTimeout(() => {
        button.textContent = original;
        button.classList.remove("copied");
      }, 1800);
    });
  });
})();
