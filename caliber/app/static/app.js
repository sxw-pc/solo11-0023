(function () {
  "use strict";

  var form = document.getElementById("run-form");
  if (!form) {
    return;
  }
  var message = document.getElementById("run-message");
  var button = form.querySelector("button[type='submit']");

  function show(text, kind) {
    message.textContent = text;
    message.className = "message " + kind;
  }

  function numberOrZero(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var selected = Array.prototype.slice
      .call(form.querySelectorAll("input[name='metrics']:checked"))
      .map(function (input) { return input.value; });
    if (selected.length === 0) {
      show("请至少选择一个指标。", "error");
      return;
    }
    var payload = {
      metrics: selected,
      datasets_dir: form.elements.datasets_dir.value,
      abs: numberOrZero(form.elements.abs.value),
      rel: numberOrZero(form.elements.rel.value)
    };
    button.disabled = true;
    button.textContent = "执行中…";
    show("正在只读执行 SQL 并比对维度…", "ok");

    fetch(form.dataset.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          var detail = result.body.message || "运行失败";
          throw new Error(detail);
        }
        var runId = result.body.run.id;
        show("运行 #" + runId + " 完成，正在打开报告…", "ok");
        window.setTimeout(function () {
          window.location.href = "/reports/" + runId;
        }, 650);
      })
      .catch(function (error) {
        show(error.message || "运行失败，请查看审计日志。", "error");
        button.disabled = false;
        button.textContent = "执行比对";
      });
  });
})();
