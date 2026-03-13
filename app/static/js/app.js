function initVoiceRecorder() {
  const root = document.querySelector("[data-voice-recorder]");
  if (!root) return;

  const startBtn = root.querySelector("[data-recorder-start]");
  const stopBtn = root.querySelector("[data-recorder-stop]");
  const resetBtn = root.querySelector("[data-recorder-reset]");
  const statusBox = root.querySelector("[data-recorder-status]");
  const previewWrap = root.querySelector("[data-recorder-preview-wrap]");
  const previewAudio = root.querySelector("[data-recorder-preview]");
  const uploadForm = root.querySelector("[data-voice-upload-form]");
  const uploadBtn = root.querySelector("[data-voice-upload-btn]");

  let mediaRecorder = null;
  let mediaStream = null;
  let recordedChunks = [];
  let recordedBlob = null;
  let previewUrl = null;

  function setStatus(message, type = "info") {
    if (!statusBox) return;
    statusBox.textContent = message;
    statusBox.className = `recorder-status recorder-status-${type}`;
    statusBox.hidden = false;
  }

  function hideStatus() {
    if (!statusBox) return;
    statusBox.hidden = true;
    statusBox.textContent = "";
    statusBox.className = "recorder-status";
  }

  function cleanupPreviewUrl() {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
  }

  function stopTracks() {
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
  }

  function updateButtons({ canStart, canStop, canReset, canUpload }) {
    if (startBtn) startBtn.disabled = !canStart;
    if (stopBtn) stopBtn.disabled = !canStop;
    if (resetBtn) resetBtn.disabled = !canReset;
    if (uploadBtn) uploadBtn.disabled = !canUpload;
  }

  function guessFileExtension(mimeType) {
    if (!mimeType) return "webm";
    if (mimeType.includes("webm")) return "webm";
    if (mimeType.includes("ogg")) return "ogg";
    if (mimeType.includes("mp4")) return "m4a";
    if (mimeType.includes("wav")) return "wav";
    return "webm";
  }

  function pickSupportedMimeType() {
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) {
      return "";
    }

    const preferred = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/ogg",
      "audio/mp4",
    ];

    for (const mimeType of preferred) {
      if (MediaRecorder.isTypeSupported(mimeType)) {
        return mimeType;
      }
    }

    return "";
  }

  function resetRecordingState({ keepStatus = false } = {}) {
    recordedChunks = [];
    recordedBlob = null;
    cleanupPreviewUrl();

    if (previewAudio) {
      previewAudio.pause();
      previewAudio.removeAttribute("src");
      previewAudio.load();
    }

    if (previewWrap) {
      previewWrap.hidden = true;
    }

    stopTracks();
    mediaRecorder = null;

    updateButtons({
      canStart: true,
      canStop: false,
      canReset: false,
      canUpload: true,
    });

    if (!keepStatus) {
      hideStatus();
    }
  }

  async function startRecording() {
    if (
      !navigator.mediaDevices ||
      !navigator.mediaDevices.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      setStatus("В этом браузере запись с микрофона недоступна.", "error");
      return;
    }

    try {
      cleanupPreviewUrl();
      recordedChunks = [];
      recordedBlob = null;

      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });

      const mimeType = pickSupportedMimeType();
      mediaRecorder = mimeType
        ? new MediaRecorder(mediaStream, { mimeType })
        : new MediaRecorder(mediaStream);

      mediaRecorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) {
          recordedChunks.push(event.data);
        }
      };

      mediaRecorder.onstop = () => {
        const finalMimeType = mediaRecorder?.mimeType || mimeType || "audio/webm";
        recordedBlob = new Blob(recordedChunks, { type: finalMimeType });

        previewUrl = URL.createObjectURL(recordedBlob);

        if (previewAudio) {
          previewAudio.src = previewUrl;
        }

        if (previewWrap) {
          previewWrap.hidden = false;
        }

        updateButtons({
          canStart: true,
          canStop: false,
          canReset: true,
          canUpload: true,
        });

        stopTracks();
        setStatus("Запись готова. Прослушай её и нажми «Отправить голосовое».", "success");
      };

      mediaRecorder.start();

      updateButtons({
        canStart: false,
        canStop: true,
        canReset: false,
        canUpload: false,
      });

      setStatus("Идёт запись... Когда закончишь, нажми «Остановить запись».", "info");
    } catch (error) {
      stopTracks();
      mediaRecorder = null;
      setStatus("Не удалось получить доступ к микрофону. Разреши доступ в браузере.", "error");
    }
  }

  function stopRecording() {
    if (!mediaRecorder) return;
    if (mediaRecorder.state !== "inactive") {
      mediaRecorder.stop();
    } else {
      stopTracks();
    }
  }

  function resetRecordedBlob() {
    resetRecordingState();
    setStatus("Запись удалена. Можно записать заново.", "info");
  }

  async function submitRecordedBlob(event) {
    if (!recordedBlob || !uploadForm) return;

    event.preventDefault();

    const mimeType = recordedBlob.type || "audio/webm";
    const extension = guessFileExtension(mimeType);
    const filename = `voice-recording.${extension}`;
    const file = new File([recordedBlob], filename, { type: mimeType });

    const formData = new FormData();
    formData.append("voice_file", file);

    updateButtons({
      canStart: false,
      canStop: false,
      canReset: false,
      canUpload: false,
    });

    setStatus("Отправляем голосовое и распознаём речь. Это может занять немного времени...", "info");

    try {
      const response = await fetch(uploadForm.action, {
        method: "POST",
        body: formData,
        credentials: "same-origin",
      });

      if (response.redirected) {
        window.location.href = response.url;
        return;
      }

      const html = await response.text();
      document.open();
      document.write(html);
      document.close();
    } catch (error) {
      updateButtons({
        canStart: true,
        canStop: false,
        canReset: true,
        canUpload: true,
      });

      setStatus("Не удалось отправить запись. Попробуй ещё раз.", "error");
    }
  }

  if (startBtn) {
    startBtn.addEventListener("click", async (event) => {
      event.preventDefault();
      await startRecording();
    });
  }

  if (stopBtn) {
    stopBtn.addEventListener("click", (event) => {
      event.preventDefault();
      stopRecording();
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener("click", (event) => {
      event.preventDefault();
      resetRecordedBlob();
    });
  }

  if (uploadForm) {
    uploadForm.addEventListener("submit", async (event) => {
      if (recordedBlob) {
        await submitRecordedBlob(event);
        return;
      }

      event.preventDefault();
      setStatus("Сначала запиши голосовое сообщение.", "error");
    });
  }

  updateButtons({
    canStart: true,
    canStop: false,
    canReset: false,
    canUpload: true,
  });
}

function initLyricsPicker() {
  const root = document.querySelector("[data-lyrics-picker]");
  if (!root) return;

  const radios = root.querySelectorAll('input[name="selected_version_public_id"]');
  const finalTextarea = root.querySelector("[data-final-lyrics-text]");

  if (!finalTextarea || !radios.length) return;

  function updateTextarea(versionId) {
    const source = root.querySelector(`[data-lyrics-source-text="${versionId}"]`);
    if (!source) return;
    finalTextarea.value = source.value;
  }

  radios.forEach((radio) => {
    radio.addEventListener("change", () => {
      updateTextarea(radio.value);
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initVoiceRecorder();
  initLyricsPicker();
});
