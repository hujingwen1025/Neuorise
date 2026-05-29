import "https://cdn.jsdelivr.net/npm/cap-widget";

document.addEventListener('contextmenu', (event) => {
  event.preventDefault();
});

const appState = {
  user: null,
  session: null,
  track: null,
  selectedRating: null,
  audio: null,
  playing: false,
  visualFrame: null,
  // Timer ID for polling track status to avoid overlapping requests
  pollTrackTimer: null,
  // Lock to prevent concurrent refreshTrackStatus calls
  isRefreshingTrack: false,
};

const audioLoadingDescriptions = ['We are still creating your audio, give us a moment...', 'Still on it! Thanks for your patience...', 'Give us a bit more time. We are still creating your audio...'];

const page = document.body.dataset.page;

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || "Something went wrong.");
  }
  return payload;
}

function $(selector) {
  return document.querySelector(selector);
}

function $all(selector) {
  return Array.from(document.querySelectorAll(selector));
}

function initialsFor(user) {
  return (user?.name || user?.email || "N")
    .split(/\s|@/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("");
}

function setMessage(selector, message, tone = "neutral") {
  const element = $(selector);
  if (!element) return;
  element.textContent = message;
  element.dataset.tone = tone;
}

async function loadUser({ redirect = false } = {}) {
  try {
    const payload = await api("/api/me");
    appState.user = payload.user;
  } catch {
    appState.user = null;
  }

  renderShell();
  if (redirect && !appState.user) {
    window.location.href = "/?auth=signup";
  }
  return appState.user;
}

function renderShell() {
  const signedOut = $(".auth-actions");
  const signedIn = $(".profile-chip");
  const startSessionButton = $("#startSessionButton");
  const heroAuthButtons = $all(".hero-actions [data-auth]");
  const initials = $("#profileInitials");
  const dashboardLinks = $all("[data-requires-auth]");

  const isSignedIn = Boolean(appState.user);
  if (signedOut) signedOut.classList.toggle("hidden", isSignedIn);
  if (signedIn) signedIn.classList.toggle("hidden", !isSignedIn);
  if (startSessionButton) startSessionButton.classList.toggle("hidden", !isSignedIn);
  heroAuthButtons.forEach((button) => button.classList.toggle("hidden", isSignedIn));
  if (initials) initials.textContent = initialsFor(appState.user);
  dashboardLinks.forEach((link) => {
    link.classList.toggle("muted-link", !isSignedIn);
  });
}

function setupAuthDialog() {
  const dialog = $("#authDialog");
  const form = $("#authForm");
  if (!dialog || !form) return;

  $all("[data-auth]").forEach((button) => {
    button.addEventListener("click", () => openAuth(button.dataset.auth));
  });

  const urlMode = new URLSearchParams(location.search).get("auth");
  const resetToken = new URLSearchParams(location.search).get("reset");
  if (resetToken) {
    openResetPassword(resetToken);
  } else if (urlMode === "signup" || urlMode === "login") {
    openAuth(urlMode);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const mode = dialog.dataset.mode || "login";
    const formData = Object.fromEntries(new FormData(form).entries());
    setMessage("#authMessage", "Checking your account...", "neutral");

    try {
      const payload = await api(`/api/${mode}`, { method: "POST", body: formData });
      if (mode === "signup") {
        setMessage("#authMessage", payload.message || "Verification email sent. Please check your inbox.", "neutral");
        form.reset();
        // keep dialog open so user can resend if needed
        const resendBtn = $("#resendVerificationButton");
        resendBtn?.classList.remove("hidden");
        startResendCooldown(60);
      } else {
        // login
        appState.user = payload.user;
        renderShell();
        dialog.close();
        form.reset();
        if (page === "home") window.location.href = "/survey.html";
      }
    } catch (error) {
      setMessage("#authMessage", error.message, "error");
      // if error suggests verification is required, show resend button
      const text = String(error.message || "").toLowerCase();
      if (text.includes("verify") || text.includes("verification")) {
        $("#resendVerificationButton").classList.remove("hidden");
      }
      const waitMatch = text.match(/wait (\d+) seconds?/);
      if (waitMatch) {
        startResendCooldown(Number(waitMatch[1]));
      }
    }
  });

  const closeButton = dialog.querySelector(".close-button");
  closeButton?.addEventListener("click", () => {
    dialog.close();
    form.reset();
    setMessage("#authMessage", "", "neutral");
    $("#resendVerificationButton")?.classList.add("hidden");
    clearResendCooldown();
  });

  const resendBtn = $("#resendVerificationButton");
  resendBtn?.addEventListener("click", async () => {
    const email = form.elements.email.value;
    if (!email) {
      setMessage("#authMessage", "Please enter your email to resend verification.", "error");
      return;
    }
    setMessage("#authMessage", "Resending verification email...", "neutral");
    try {
      const resp = await api("/api/resend-verification", { method: "POST", body: { email } });
      setMessage("#authMessage", resp.message || "Verification email resent.", "neutral");
      startResendCooldown(60);
    } catch (err) {
      setMessage("#authMessage", err.message, "error");
      const text = String(err.message || "").toLowerCase();
      const waitMatch = text.match(/wait (\d+) seconds?/);
      if (waitMatch) {
        startResendCooldown(Number(waitMatch[1]));
      }
    }
  });

  const forgotPasswordLinkLogin = $("#forgotPasswordLinkLogin");
  forgotPasswordLinkLogin?.addEventListener("click", (e) => {
    e.preventDefault();
    dialog.close();
    $("#forgotPasswordDialog")?.showModal();
  });

  // Forgot password dialog
  const forgotPasswordDialog = $("#forgotPasswordDialog");
  const forgotPasswordForm = $("#forgotPasswordForm");
  if (forgotPasswordDialog && forgotPasswordForm) {
    forgotPasswordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = Object.fromEntries(new FormData(forgotPasswordForm).entries());
      const email = (formData.email || "").trim().toLowerCase();
      if (!email) {
        setMessage("#forgotPasswordMessage", "Please enter your email.", "error");
        return;
      }
      if (!formData["cap-token"]) {
        setMessage("#forgotPasswordMessage", "Please complete the captcha.", "error");
        return;
      }
      setMessage("#forgotPasswordMessage", "Sending password reset link...", "neutral");
      try {
        const resp = await api("/api/forgot-password", { method: "POST", body: { ...formData, email } });
        setMessage("#forgotPasswordMessage", resp.message || "Password reset link sent. Check your email.", "neutral");
        forgotPasswordForm.reset();
        window.setTimeout(() => {
          forgotPasswordDialog.close();
        }, 2000);
      } catch (err) {
        setMessage("#forgotPasswordMessage", err.message, "error");
      }
    });

    const backToLoginButton = $("#backToLoginButton");
    backToLoginButton?.addEventListener("click", (e) => {
      e.preventDefault();
      forgotPasswordDialog.close();
      openAuth("login");
    });

    const closeForgotButton = forgotPasswordDialog.querySelector(".close-button");
    closeForgotButton?.addEventListener("click", () => {
      forgotPasswordDialog.close();
      forgotPasswordForm.reset();
      setMessage("#forgotPasswordMessage", "", "neutral");
    });
  }

  // Reset password dialog
  const resetPasswordDialog = $("#resetPasswordDialog");
  const resetPasswordForm = $("#resetPasswordForm");
  if (resetPasswordDialog && resetPasswordForm) {
    resetPasswordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const password = resetPasswordForm.elements.password.value || "";
      const confirmPassword = resetPasswordForm.elements.confirmPassword.value || "";
      const token = resetPasswordForm.dataset.token || "";

      if (password !== confirmPassword) {
        setMessage("#resetPasswordMessage", "Passwords do not match.", "error");
        return;
      }
      if (password.length < 8) {
        setMessage("#resetPasswordMessage", "Password must be at least 8 characters.", "error");
        return;
      }

      setMessage("#resetPasswordMessage", "Resetting your password...", "neutral");
      try {
        const resp = await api("/api/reset-password", { method: "POST", body: { token, password } });
        setMessage("#resetPasswordMessage", resp.message || "Password reset successfully. Redirecting to login...", "neutral");
        resetPasswordForm.reset();
        window.setTimeout(() => {
          resetPasswordDialog.close();
          openAuth("login");
        }, 2000);
      } catch (err) {
        setMessage("#resetPasswordMessage", err.message, "error");
      }
    });

    const closeResetButton = resetPasswordDialog.querySelector(".close-button");
    closeResetButton?.addEventListener("click", () => {
      resetPasswordDialog.close();
      resetPasswordForm.reset();
      setMessage("#resetPasswordMessage", "", "neutral");
    });
  }
}

function startResendCooldown(seconds, button = $("#resendVerificationButton")) {
  const btn = button;
  if (!btn) return;
  clearResendCooldown(btn);
  btn.disabled = true;
  const defaultText = btn.dataset.defaultText || btn.textContent || "Resend verification email";
  btn.dataset.defaultText = defaultText;
  let remaining = seconds;
  btn.textContent = `${defaultText} (${remaining}s)`;
  const timer = window.setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearResendCooldown(btn);
      return;
    }
    btn.textContent = `${defaultText} (${remaining}s)`;
  }, 1000);
  btn.dataset.resendTimer = String(timer);
}

function clearResendCooldown(button = $("#resendVerificationButton")) {
  const btn = button;
  if (!btn) return;
  if (btn.dataset.resendTimer) {
    clearInterval(Number(btn.dataset.resendTimer));
    delete btn.dataset.resendTimer;
  }
  btn.disabled = false;
  btn.textContent = btn.dataset.defaultText || "Resend verification email";
}

function openResetPassword(token) {
  const dialog = $("#resetPasswordDialog");
  const form = $("#resetPasswordForm");
  if (!dialog || !form) return;
  form.dataset.token = token;
  setMessage("#resetPasswordMessage", "", "neutral");
  form.reset();
  dialog.showModal();
}

function openAuth(mode) {
  const dialog = $("#authDialog");
  const form = $("#authForm");
  if (!dialog || !form) return;

  dialog.dataset.mode = mode;
  $("#authTitle").textContent = mode === "signup" ? "Create your account" : "Log in";
  $("#authModeLabel").textContent = mode === "signup" ? "Begin saving sessions" : "Welcome back";
  form.elements.name.parentElement.classList.toggle("hidden", mode !== "signup");
  $("#forgotPasswordLinkLogin")?.classList.toggle("hidden", mode !== "login");
  setMessage("#authMessage", "", "neutral");
  $("#resendVerificationButton")?.classList.add("hidden");
  form.reset();
  dialog.showModal();
}

function setupLogout() {
  const button = $("#logoutButton");
  if (!button) return;
  button.addEventListener("click", async () => {
    await api("/api/logout", { method: "POST", body: {} });
    appState.user = null;
    renderShell();
    window.location.href = "/";
  });
}

function setupProfileNavigation() {
  const initials = $("#profileInitials");
  if (!initials) return;
  initials.tabIndex = 0;
  initials.setAttribute("role", "button");
  initials.addEventListener("click", () => {
    window.location.href = "/profile.html";
  });
  initials.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      window.location.href = "/profile.html";
    }
  });
}

function setupVerifyResultPage() {
  const title = $("#verifyTitle");
  const message = $("#verifyMessage");
  const actionButton = $("#verifyActionButton");
  const params = new URLSearchParams(window.location.search);
  const status = params.get("status") || "success";
  const rawMessage = params.get("message") || "Your email has been verified.";
  const text = decodeURIComponent(rawMessage.replace(/\+/g, " "));

  if (status === "success") {
    if (title) title.textContent = "Email verified successfully.";
    if (message) message.textContent = text;
    if (actionButton) {
      actionButton.textContent = "Go to dashboard";
      actionButton.href = "/sessions.html";
    }
  } else {
    if (title) title.textContent = "Email verification failed.";
    if (message) message.textContent = text;
    if (actionButton) {
      actionButton.textContent = "Go to home";
      actionButton.href = "/";
    }
  }
}

function setupProfilePage() {
  const form = $("#profileForm");
  if (!form) return;
  const nameInput = form.elements.name;
  const emailInput = form.elements.email;
  const message = $("#profileMessage");

  const emailStatus = $("#emailStatus");
  const resendProfileButton = $("#resendProfileVerificationButton");

  function refreshEmailStatus() {
    if (!appState.user || !emailStatus) return;
    if (!appState.user.verified) {
      emailStatus.textContent = "Your email address is not verified. Please verify to keep your account active.";
      emailStatus.dataset.tone = "warning";
      resendProfileButton?.classList.remove("hidden");
    } else {
      emailStatus.textContent = "Your email is verified.";
      emailStatus.dataset.tone = "neutral";
      resendProfileButton?.classList.add("hidden");
    }
  }

  if (appState.user) {
    if (nameInput) nameInput.value = appState.user.name || "";
    if (emailInput) emailInput.value = appState.user.email || "";
  }
  refreshEmailStatus();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setMessage("#profileMessage", "Saving your changes...", "neutral");
    const payload = collectForm(form);

    try {
      const response = await api("/api/me", { method: "PATCH", body: payload });
      appState.user = response.user;
      renderShell();
      refreshEmailStatus();
      setMessage("#profileMessage", response.message || "Profile updated successfully.", "neutral");
      form.password.value = "";
      if (!appState.user.verified) {
        resendProfileButton?.classList.remove("hidden");
      }
    } catch (error) {
      setMessage("#profileMessage", error.message, "error");
    }
  });

  resendProfileButton?.addEventListener("click", async () => {
    const email = emailInput?.value?.trim().toLowerCase();
    if (!email) {
      setMessage("#profileMessage", "Please enter your email to resend verification.", "error");
      return;
    }
    setMessage("#profileMessage", "Resending verification email...", "neutral");
    try {
      const resp = await api("/api/resend-verification", { method: "POST", body: { email } });
      setMessage("#profileMessage", resp.message || "Verification email resent.", "neutral");
      startResendCooldown(60, resendProfileButton);
    } catch (err) {
      setMessage("#profileMessage", err.message, "error");
      const text = String(err.message || "").toLowerCase();
      const waitMatch = text.match(/wait (\d+) seconds?/);
      if (waitMatch) {
        startResendCooldown(Number(waitMatch[1]), resendProfileButton);
      }
    }
  });

  const deleteAccountButton = $("#deleteAccountButton");
  deleteAccountButton?.addEventListener("click", async () => {
    const confirmed = confirm(
      `Are you sure you want to delete your account? This will permanently remove all your data including sessions, tracks, and feedback.\n\nThis action cannot be undone.`
    );
    if (!confirmed) return;
    const doubleConfirmed = prompt(
      `To confirm, please type your email address: ${appState.user?.email || ""}`
    );
    if (doubleConfirmed !== appState.user?.email) {
      setMessage("#deleteMessage", "Email did not match. Account deletion cancelled.", "error");
      return;
    }
    setMessage("#deleteMessage", "Deleting your account...", "neutral");
    try {
      await api("/api/me", { method: "DELETE" });
      setMessage("#deleteMessage", "Account deleted successfully. Redirecting...", "neutral");
      window.setTimeout(() => {
        window.location.href = "/";
      }, 1500);
    } catch (err) {
      setMessage("#deleteMessage", err.message, "error");
    }
  });
}

function collectForm(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function normalizeIntake(raw) {
  return {
    mood: raw.mood,
    energy: Number(raw.energy),
    stress: Number(raw.stress),
    need: raw.need,
    texture: raw.texture,
    avoid: raw.avoid || "",
    instructions: raw.instructions || "",
    symptomRatings: {
      sadness: Number(raw.sadness),
      noInterest: Number(raw.noInterest),
      deathThoughts: Number(raw.deathThoughts),
      hopelessness: Number(raw.hopelessness),
      anxious: Number(raw.anxious),
      excessiveWorry: Number(raw.excessiveWorry),
      panicAttacks: Number(raw.panicAttacks),
      fatigue: Number(raw.fatigue),
      concentration: Number(raw.concentration),
      difficultySleeping: Number(raw.difficultySleeping),
      disturbedSleep: Number(raw.disturbedSleep),
      irritable: Number(raw.irritable),
    },
    heartRate: Number(raw.heartRate),
    breathRate: Number(raw.breathRate),
    duration: Number(raw.duration),
  };
}

function setupRanges() {
  $all('input[type="range"]').forEach((range) => {
    const readout = document.querySelector(`[data-for="${range.name}"]`);
    if (!readout) return;
    range.addEventListener("input", () => {
      readout.textContent = range.value;
    });
  });
}

function setupSurveyPage() {
  const form = $("#intakeForm");
  if (!form) return;
  setupRanges();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton?.textContent;

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Generating...";
    }

    setMessage("#surveyMessage", "Generating and saving your session. Please wait ...", "neutral");
    const formData = collectForm(form);
    const intake = normalizeIntake(formData);
    const captchaToken = formData["cap-token"];

    try {
      const payload = await api("/api/sessions", { 
        method: "POST", 
        body: { 
          intake,
          captchaToken
        } 
      });
      window.location.href = `/generation.html?session=${payload.session.id}`;
    } catch (error) {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText || "Generate and save session";
      }
      setMessage("#surveyMessage", error.message, "error");
    }
  });
}

function getSessionId() {
  return new URLSearchParams(location.search).get("session");
}

function scheduleTrackRefresh(trackId, delay = 5000) {
  if (!trackId) return;
  if (appState.pollTrackTimer) {
    clearTimeout(appState.pollTrackTimer);
    appState.pollTrackTimer = null;
  }
  appState.pollTrackTimer = window.setTimeout(() => refreshTrackStatus(trackId), delay);
}

function clearTrackRefresh() {
  if (appState.pollTrackTimer) {
    clearTimeout(appState.pollTrackTimer);
    appState.pollTrackTimer = null;
  }
}

async function loadSession() {
  const sessionId = getSessionId();
  if (!sessionId) {
    window.location.href = "/survey.html";
    return null;
  }
  const payload = await api(`/api/sessions/${sessionId}`);
  appState.session = payload.session;
  appState.track = payload.session.tracks[payload.session.tracks.length - 1];
  return payload.session;
}

function renderGeneration(session, loadSessionTitle = false) {
  const track = getSelectedTrack(session);
  appState.track = track;
  appState.selectedTrackId = track?.id;
  const versionLabel = track?.version ? `Version ${track.version}` : `Version ${session.tracks.indexOf(track) + 1}`;
  $("#trackTitle").textContent = track?.title || "Loading track";
  $("#trackSubtitle").textContent = `${session.intake.texture} tuned for ${session.intake.mood.toLowerCase()} mood, ${session.intake.heartRate} bpm heart rate, and ${session.intake.breathRate} breaths/min. · ${versionLabel} · ${formatTrackStatus(track?.status)}`;
  renderSessionTitle(session);
  // Gemini plan and Suno request UI removed; no DOM updates required here.
  $("#feedbackLink").href = `/feedback.html?session=${session.id}`;
  configureAudio(track?.audio_config, track?.audio_url);
  const placeholder = $("#playerPlaceholder");
  const iframe = $("#playerIframe");
  const hasAudio = Boolean(track?.audio_url);

  if (placeholder) placeholder.classList.toggle("hidden", hasAudio);
  if (iframe) iframe.classList.toggle("hidden", !hasAudio);

  if (hasAudio && iframe) {
    const songName = encodeURIComponent(track.title || "");
    const songUrl = encodeURIComponent(track.audio_url || "");
    iframe.src = `/player/index.html?songName=${songName}&songUrl=${songUrl}`;
    setMessage("#generationMessage", "Suno audio is ready to play.", "neutral");
  } else {
    if (iframe) iframe.src = "/player/index.html";
    setMessage("#generationMessage", "Suno is generating this track. Please wait ...", "neutral");
  }

  renderTrackHistory(session);
  drawIdleVisualizer();
  if (track.provider_task_id && !track.audio_url && !["SUCCESS", "COMPLETE", "completed"].includes(track.status)) {
    scheduleTrackRefresh(track.id, 5000);
  }
}

function getSelectedTrack(session) {
  if (!session?.tracks?.length) return null;
  if (appState.selectedTrackId) {
    const selected = session.tracks.find((track) => String(track.id) === String(appState.selectedTrackId));
    if (selected) return selected;
  }

  return session.tracks[session.tracks.length - 1];
}

function renderSessionTitle(session) {
  const title = session.title || session.tracks?.[0]?.title || "Untitled session";
  const input = $("#sessionTitleInput");
  if (input === document.activeElement) return;
  if (input) input.value = title;
}

let sessionTitleCheckTimer = null;

function showSessionTitleCheck() {
  const check = $("#sessionTitleSuccess");
  if (!check) return;
  check.classList.add("visible");
  if (sessionTitleCheckTimer) {
    clearTimeout(sessionTitleCheckTimer);
  }
  sessionTitleCheckTimer = window.setTimeout(() => {
    check.classList.remove("visible");
    sessionTitleCheckTimer = null;
  }, 3000);
}

async function saveSessionTitle() {
  if (!appState.session) return;
  const input = $("#sessionTitleInput");
  if (!input) return;
  const title = input.value.trim();
  if (!title) {
    input.value = appState.session.title || "";
    return;
  }
  if (title === appState.session.title) {
    return;
  }

  try {
    const payload = await api(`/api/sessions/${appState.session.id}`, {
      method: "PATCH",
      body: { title },
    });
    appState.session = payload.session;
    renderSessionTitle(appState.session);
    showSessionTitleCheck();
  } catch (error) {
    console.error(error);
    input.value = appState.session.title || "";
  }
}

function setupSessionTitleControls() {
  const input = $("#sessionTitleInput");
  if (!input) return;
  input.addEventListener("blur", saveSessionTitle);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveSessionTitle();
    }
  });
}

function formatTrackStatus(status) {
  if (!status) return "Unknown";
  const normalized = String(status).toUpperCase();
  if (normalized === "SUCCESS" || normalized === "COMPLETE" || normalized === "COMPLETED") return "Ready to play";
  if (normalized === "PENDING") return "Queued";
  if (normalized === "TEXT_SUCCESS") return "Processing";
  if (normalized === "FIRST_SUCCESS") return "Rendering";
  return normalized.charAt(0) + normalized.slice(1).toLowerCase().replace(/_/g, " ");
}

function renderTrackHistory(session) {
  const timeline = $("#versionTimeline");
  if (!timeline || !session?.tracks?.length) return;

  const sortedTracks = [...session.tracks].sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
  const selectedTrackId = String(appState.selectedTrackId || sortedTracks[sortedTracks.length - 1]?.id);

  timeline.innerHTML = sortedTracks
    .map((track, index) => {
      const isActive = String(track.id) === selectedTrackId;
      const versionLabel = track.version ? `Version ${track.version}` : `Version ${index + 1}`;
      const statusText = track.audio_url ? "Ready to play" : formatTrackStatus(track.status);
      return `<button type="button" class="version-item${isActive ? " active" : ""}" data-track-id="${track.id}">
        <div class="version-row">
          <div>
            <strong>${escapeHtml(versionLabel)}</strong>
            <p>${escapeHtml(track.title || "Untitled version")}</p>
          </div>
          <span class="status-pill">${escapeHtml(statusText)}</span>
        </div>
        <small>${new Date(track.created_at).toLocaleString()}</small>
      </button>`;
    })
    .join("");

  $all(".version-item").forEach((button) => {
    button.addEventListener("click", () => selectTrackVersion(button.dataset.trackId));
  });
}

function selectTrackVersion(trackId) {
  if (!appState.session) return;
  const track = appState.session.tracks.find((item) => String(item.id) === String(trackId));
  if (!track) return;
  appState.selectedTrackId = track.id;
  appState.track = track;
  renderGeneration(appState.session);
}

async function refreshTrackStatus(trackId) {
  if (!trackId) return;
  // If a refresh is already in progress, schedule a retry and bail out.
  if (appState.isRefreshingTrack) {
    scheduleTrackRefresh(trackId, 1000);
    return;
  }

  appState.isRefreshingTrack = true;
  // Clear any pending scheduled refresh since we're about to perform one now.
  clearTrackRefresh();
  setMessage("#generationMessage", "Suno is generating this track. Please wait ...", "neutral");
  setMessage("#placeholderDescription", audioLoadingDescriptions[Math.floor(Math.random() * audioLoadingDescriptions.length)]);

  try {
    const payload = await api(`/api/tracks/${trackId}/refresh`);
    appState.session = payload.session;
    appState.track = payload.session.tracks[payload.session.tracks.length - 1];
    renderGeneration(payload.session);
    if (!appState.track.audio_url && ["PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"].includes(appState.track.status)) {
      // Poll every 5 seconds to match server's MIN_POLL_INTERVAL_SECONDS
      scheduleTrackRefresh(appState.track.id, 5000);
    } else if (appState.track.audio_url) {
      setMessage("#generationMessage", "Suno audio is ready to play.", "neutral");
    }
  } catch (error) {
    setMessage("#generationMessage", error.message, "error");
    // Retry after a short delay in case this was a transient error
    scheduleTrackRefresh(trackId, 5000);
  } finally {
    appState.isRefreshingTrack = false;
  }
}

async function setupGenerationPage() {
  try {
    const session = await loadSession();
    if (session) {
      renderGeneration(session);
      setupSessionTitleControls();
    }
  } catch (error) {
    setMessage("#generationMessage", error.message, "error");
  }

  // Copy prompt button removed.

  $("#playButton")?.addEventListener("click", playAudio);
  $("#pauseButton")?.addEventListener("click", pauseAudio);
  $("#forwardButton")?.addEventListener("click", () => seekRelative(10));
  $("#backwardButton")?.addEventListener("click", () => seekRelative(-10));

  const progressTrack = $("#progressTrack");
  progressTrack?.addEventListener("click", (event) => {
    const rect = progressTrack.getBoundingClientRect();
    const percent = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    seekPercent(percent);
  });
  progressTrack?.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") {
      event.preventDefault();
      seekRelative(5);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      seekRelative(-5);
    } else if (event.key === "Home") {
      event.preventDefault();
      seekPercent(0);
    } else if (event.key === "End") {
      event.preventDefault();
      seekPercent(1);
    }
  });

  $("#skipButton")?.addEventListener("click", async () => {
    if (!appState.session || !appState.track) return;
    await submitFeedback({
      rating: "down",
      feedbackText: "User skipped the previous track quickly.",
      skipped: true,
      redirectToFeedback: false,
    });
  });
}

function renderFeedback(session) {
  const track = session.tracks[session.tracks.length - 1];
  renderSessionTitle(session);
  $("#feedbackTrackTitle").textContent = track.title;
  $("#feedbackTrackMeta").textContent = `${session.intake.need} for ${session.intake.mood}; latest version ${track.version}.`;
  $("#backToGeneration").href = `/generation.html?session=${session.id}`;
  renderHistory(session);
}

function renderHistory(session) {
  const history = $("#historyList");
  if (!history) return;
  const feedback = session.feedback || [];
  history.innerHTML = feedback.length
    ? feedback
        .map(
          (item) => `<article class="history-item">
            <strong>${item.rating === "up" ? "Helpful" : "Needs adjustment"}</strong>
            <p>${escapeHtml(item.feedback_text || "No written note provided.")}</p>
            <small>${new Date(item.created_at).toLocaleString()}</small>
          </article>`,
        )
        .join("")
    : `<article class="history-item"><strong>No feedback yet</strong><p>Your ratings and notes will appear here.</p></article>`;
}

async function setupFeedbackPage() {
  try {
    const session = await loadSession();
    if (session) {
      renderFeedback(session);
      setupSessionTitleControls();
    }
  } catch (error) {
    setMessage("#feedbackMessage", error.message, "error");
  }

  $all(".rating-button").forEach((button) => {
    button.addEventListener("click", () => {
      appState.selectedRating = button.dataset.rating;
      $all(".rating-button").forEach((other) => {
        other.classList.toggle("active", other === button);
      });
    });
  });

  $("#feedbackForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const originalText = submitButton?.textContent;

    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Generating...";
    }

    const formValues = collectForm(form);
    try {
      await submitFeedback({
        rating: appState.selectedRating || "down",
        feedbackText: formValues.feedbackText || "",
        skipped: false,
        redirectToFeedback: true,
        captchaToken: formValues["cap-token"],
        submitButton,
        submitButtonText: originalText,
      });
    } catch (error) {
      // Errors are handled inside submitFeedback.
    }
  });
}

async function submitFeedback({ rating, feedbackText, skipped, redirectToFeedback, captchaToken, submitButton, submitButtonText }) {
  if (!appState.session || !appState.track) return;
  setMessage("#feedbackMessage", "Saving feedback and generating the next version. Please wait ...", "neutral");
  setMessage("#generationMessage", "Saving skip and generating a revised track. Please wait ...", "neutral");

  try {
    const payload = await api(`/api/sessions/${appState.session.id}/feedback`, {
      method: "POST",
      body: {
        trackId: appState.track.id,
        rating,
        feedbackText,
        skipped,
        captchaToken,
      },
    });
    appState.session = payload.session;
    appState.track = payload.session.tracks[payload.session.tracks.length - 1];
    if (redirectToFeedback) {
      window.location.href = `/generation.html?session=${appState.session.id}`;
    } else {
      renderGeneration(appState.session);
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalText || "Generate improved track";
      }
    }
  } catch (error) {
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.textContent = originalText || "Generate improved track";
    }
    setMessage("#feedbackMessage", error.message, "error");
    setMessage("#generationMessage", error.message, "error");
    throw error;
  }
}

async function setupSessionsPage() {
  await renderSessionsList();
}

async function renderSessionsList() {
  try {
    const payload = await api("/api/sessions");
    const list = $("#sessionsList");
    list.innerHTML = payload.sessions.length
      ? payload.sessions
          .map(
            (session) => `<div class="session-row-wrapper" data-session-id="${session.id}">
              <a class="session-row" href="/generation.html?session=${session.id}">
                <span>
                  <strong>${escapeHtml(session.title)}</strong>
                  <small>${escapeHtml(session.mood)} · ${escapeHtml(session.need)} · ${new Date(session.created_at).toLocaleString()}</small>
                </span>
                <span class="status-pill">${session.track_count} track${session.track_count === 1 ? "" : "s"}</span>
              </a>
              <button class="icon-button delete-session-button" aria-label="Delete session" title="Delete session">×</button>
            </div>`,
          )
          .join("")
      : `<article class="history-item"><strong>No saved sessions yet</strong><p>Start with the survey to create your first track.</p></article>`;
    
    // Add event listeners to delete buttons
    $all(".delete-session-button").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const wrapper = button.closest(".session-row-wrapper");
        const sessionId = wrapper.dataset.sessionId;
        const sessionTitle = wrapper.querySelector("strong").textContent;
        
        if (confirm(`Are you sure you want to delete "${sessionTitle}"? This action cannot be undone.`)) {
          await deleteSession(sessionId);
        }
      });
    });
  } catch (error) {
    setMessage("#sessionsMessage", error.message, "error");
  }
}

async function deleteSession(sessionId) {
  try {
    await api(`/api/sessions/${sessionId}`, { method: "DELETE" });
    setMessage("#sessionsMessage", "Session deleted successfully.", "success");
    await renderSessionsList();
  } catch (error) {
    setMessage("#sessionsMessage", error.message, "error");
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => {
    const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
    return entities[char];
  });
}

function configureAudio(config, audioUrl = null) {
  stopAudio();
  appState.audio = {
    baseTempo: config?.tempo || 58,
    intensity: config?.intensity || 0.5,
    feedbackWarmth: config?.warmth || 0.9,
    root: config?.root || 246.94,
    audioUrl,
    htmlAudio: null,
    context: null,
    nodes: [],
    analyser: null,
  };
  updateProgressUI();
}

function createTone(context, destination, frequency, gainValue, type = "sine") {
  const osc = context.createOscillator();
  const gain = context.createGain();
  osc.type = type;
  osc.frequency.value = frequency;
  gain.gain.value = gainValue;
  osc.connect(gain).connect(destination);
  osc.start();
  return [osc, gain];
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const minutes = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${minutes}:${secs.toString().padStart(2, "0")}`;
}

function updateProgressUI() {
  const currentTimeLabel = $("#currentTime");
  const durationLabel = $("#durationTime");
  const fill = $("#progressFill");
  if (!currentTimeLabel || !durationLabel || !fill) return;

  const audio = appState.audio?.htmlAudio;
  if (audio && audio.duration && !Number.isNaN(audio.duration)) {
    const current = Math.min(audio.currentTime, audio.duration);
    const percent = audio.duration ? (current / audio.duration) * 100 : 0;
    currentTimeLabel.textContent = formatTime(current);
    durationLabel.textContent = formatTime(audio.duration);
    fill.style.width = `${percent}%`;
  } else {
    currentTimeLabel.textContent = "0:00";
    durationLabel.textContent = audio?.duration ? formatTime(audio.duration) : "0:00";
    fill.style.width = "0%";
  }
}

function seekPercent(percent) {
  const audio = appState.audio?.htmlAudio;
  if (!audio || !audio.duration || Number.isNaN(audio.duration)) return;
  audio.currentTime = Math.min(audio.duration, Math.max(0, percent * audio.duration));
  updateProgressUI();
}

function seekRelative(seconds) {
  const audio = appState.audio?.htmlAudio;
  if (!audio || !audio.duration || Number.isNaN(audio.duration)) return;
  const target = Math.min(audio.duration, Math.max(0, audio.currentTime + seconds));
  audio.currentTime = target;
  updateProgressUI();
}

function playAudio() {
  if (!appState.audio) return;
  if (appState.audio.htmlAudio && !appState.playing) {
    appState.audio.htmlAudio.play();
    appState.playing = true;
    animatePreviewVisualizer();
    return;
  }

  if (appState.audio.audioUrl) {
    console.log(appState.audio.audioUrl)
    const audio = new Audio(appState.audio.audioUrl);
    audio.crossOrigin = "anonymous";
    audio.addEventListener("ended", () => {
      appState.playing = false;
      updateProgressUI();
      drawIdleVisualizer();
    });
    audio.addEventListener("timeupdate", updateProgressUI);
    audio.addEventListener("loadedmetadata", updateProgressUI);
    audio.addEventListener("play", updateProgressUI);
    audio.addEventListener("pause", updateProgressUI);
    appState.audio.htmlAudio = audio;
    appState.playing = true;
    audio.play();
    animatePreviewVisualizer();
    return;
  }

  const context = new AudioContext();
  const master = context.createGain();
  const analyser = context.createAnalyser();
  const filter = context.createBiquadFilter();
  const delay = context.createDelay();
  const delayGain = context.createGain();
  const compressor = context.createDynamicsCompressor();

  master.gain.value = 0.0001;
  filter.type = "lowpass";
  filter.frequency.value = 1400 * appState.audio.feedbackWarmth;
  delay.delayTime.value = 0.42;
  delayGain.gain.value = 0.18;
  analyser.fftSize = 512;

  master.connect(filter).connect(compressor).connect(analyser).connect(context.destination);
  master.connect(delay).connect(delayGain).connect(filter);

  const root = appState.audio.root;
  const chord = [1, 1.5, 2, 2.5].map((ratio, index) =>
    createTone(context, master, root * ratio, 0.025 * appState.audio.intensity * (1 - index * 0.12), index % 2 ? "triangle" : "sine"),
  );
  const breath = createTone(context, master, root / 2, 0.018, "sine");
  const shimmer = createTone(context, master, root * 4, 0.006, "sine");

  const now = context.currentTime;
  master.gain.exponentialRampToValueAtTime(0.42, now + 2.5);
  breath[1].gain.setValueAtTime(0.01, now);

  const beatSeconds = 60 / appState.audio.baseTempo;
  const pulseTimer = window.setInterval(() => {
    const t = context.currentTime;
    breath[1].gain.cancelScheduledValues(t);
    breath[1].gain.setValueAtTime(0.008, t);
    breath[1].gain.linearRampToValueAtTime(0.045 * appState.audio.intensity, t + beatSeconds * 1.6);
    breath[1].gain.linearRampToValueAtTime(0.008, t + beatSeconds * 3.8);
    shimmer[0].frequency.setValueAtTime(root * (3 + Math.random() * 1.5), t);
  }, beatSeconds * 4000);

  appState.audio.context = context;
  appState.audio.nodes = [...chord.flat(), ...breath, ...shimmer, master, filter, delay, delayGain, compressor, pulseTimer];
  appState.audio.analyser = analyser;
  appState.playing = true;
  animateVisualizer();
}

function stopAudio() {
  if (appState.visualFrame) {
    cancelAnimationFrame(appState.visualFrame);
    appState.visualFrame = null;
  }
  if (!appState.audio) return;
  if (appState.audio.htmlAudio) {
    appState.audio.htmlAudio.pause();
    appState.audio.htmlAudio.currentTime = 0;
    appState.audio.htmlAudio = null;
  }
  for (const node of appState.audio.nodes || []) {
    if (typeof node === "number") window.clearInterval(node);
    if (node?.stop) {
      try {
        node.stop();
      } catch {
        // Oscillator may already be stopped.
      }
    }
    if (node?.disconnect) node.disconnect();
  }
  if (appState.audio.context) appState.audio.context.close();
  appState.playing = false;
}

function pauseAudio() {
  if (appState.audio?.htmlAudio) {
    appState.audio.htmlAudio.pause();
    appState.playing = false;
    updateProgressUI();
    return;
  }

  stopAudio();
  if (appState.track) configureAudio(appState.track.audio_config, appState.track.audio_url);
  drawIdleVisualizer();
}

function drawIdleVisualizer() {
  const canvas = $("#visualizer");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "rgba(146, 216, 196, 0.95)";
  ctx.lineWidth = 3;
  ctx.beginPath();
  for (let x = 0; x < canvas.width; x += 10) {
    const y = canvas.height / 2 + Math.sin(x / 34) * 18 + Math.cos(x / 80) * 10;
    if (x === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function animateVisualizer() {
  const canvas = $("#visualizer");
  const analyser = appState.audio?.analyser;
  if (!canvas || !analyser) return;

  const ctx = canvas.getContext("2d");
  const data = new Uint8Array(analyser.frequencyBinCount);

  function render() {
    analyser.getByteFrequencyData(data);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const gradient = ctx.createLinearGradient(0, 0, canvas.width, 0);
    gradient.addColorStop(0, "#92d8c4");
    gradient.addColorStop(0.48, "#e7bb63");
    gradient.addColorStop(1, "#e9806e");
    ctx.strokeStyle = gradient;
    ctx.lineWidth = 3;
    ctx.beginPath();

    for (let i = 0; i < data.length; i += 1) {
      const x = (i / (data.length - 1)) * canvas.width;
      const y = canvas.height - 24 - (data[i] / 255) * 96 - Math.sin(i / 8 + performance.now() / 900) * 8;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }

    ctx.stroke();
    appState.visualFrame = requestAnimationFrame(render);
  }

  render();
}

function animatePreviewVisualizer() {
  const canvas = $("#visualizer");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  function render() {
    if (!appState.playing) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const gradient = ctx.createLinearGradient(0, 0, canvas.width, 0);
    gradient.addColorStop(0, "#92d8c4");
    gradient.addColorStop(0.52, "#e7bb63");
    gradient.addColorStop(1, "#5d8cc9");
    ctx.strokeStyle = gradient;
    ctx.lineWidth = 3;
    ctx.beginPath();
    for (let x = 0; x < canvas.width; x += 9) {
      const y =
        canvas.height / 2 +
        Math.sin(x / 28 + performance.now() / 650) * 24 +
        Math.cos(x / 74 + performance.now() / 1200) * 11;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    appState.visualFrame = requestAnimationFrame(render);
  }

  render();
}

setupAuthDialog();
setupLogout();
setupProfileNavigation();

if (page === "home") {
  loadUser();
}

if (["profile", "survey", "generation", "feedback", "sessions"].includes(page)) {
  await loadUser({ redirect: true });
}

if (page === "profile") setupProfilePage();
if (page === "survey") setupSurveyPage();
if (page === "generation") setupGenerationPage();
if (page === "feedback") setupFeedbackPage();
if (page === "sessions") setupSessionsPage();
if (page === "verify-result") setupVerifyResultPage();
