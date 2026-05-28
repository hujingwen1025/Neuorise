const musicContainer = document.getElementById("music-container");
const playButton = document.getElementById("play");
const prevButton = document.getElementById("prev");
const nextButton = document.getElementById("next");
const audio = document.getElementById("audio");
const progress = document.getElementById("progress");
const progressContainer = document.getElementById("progress-container");
const title = document.getElementById("title");
const cover = document.getElementById("cover");

const defaultSongName = "No Song";
const defaultSongUrl = "./audio.mp3";
const defaultCoverUrl = "./vinyl.png";
const skipSeconds = 10;

function getQueryParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function normalizeTime(value) {
  return Number.isFinite(value) ? value : 0;
}

function loadSong(songTitle, songUrl, coverUrl) {
  title.innerText = songTitle || defaultSongName;
  audio.src = songUrl || defaultSongUrl;
  cover.src = coverUrl || defaultCoverUrl;
}

function playSong() {
  musicContainer.classList.add("play");
  playButton.querySelector("i.fas").classList.remove("fa-play");
  playButton.querySelector("i.fas").classList.add("fa-pause");
  audio.play();
}

function pauseSong() {
  musicContainer.classList.remove("play");
  playButton.querySelector("i.fas").classList.remove("fa-pause");
  playButton.querySelector("i.fas").classList.add("fa-play");
  audio.pause();
}

function rewind() {
  const currentTime = normalizeTime(audio.currentTime);
  audio.currentTime = Math.max(0, currentTime - skipSeconds);
}

function fastForward() {
  const duration = normalizeTime(audio.duration);
  const currentTime = normalizeTime(audio.currentTime);
  audio.currentTime = Math.min(duration, currentTime + skipSeconds);
}

function updateProgress(e) {
  const { duration, currentTime } = e.srcElement;
  const progressPercent = Number.isFinite(duration) && Number.isFinite(currentTime)
    ? (currentTime / duration) * 100
    : 0;
  progress.style.width = `${progressPercent}%`;
}

function setProgress(e) {
  const width = this.clientWidth;
  const clickX = e.offsetX;
  const duration = audio.duration;
  if (width > 0 && Number.isFinite(duration) && duration > 0) {
    const newTime = (clickX / width) * duration;
    audio.currentTime = normalizeTime(newTime);
  }
}

// Event Listeners
playButton.addEventListener("click", () => {
  const isPlaying = musicContainer.classList.contains("play");
  isPlaying ? pauseSong() : playSong();
});

prevButton.addEventListener("click", rewind);
nextButton.addEventListener("click", fastForward);

audio.addEventListener("timeupdate", updateProgress);
progressContainer.addEventListener("click", setProgress);

audio.addEventListener("ended", () => {
  pauseSong();
  audio.currentTime = 0;
});

// Init
const songTitle = getQueryParam("songName") || defaultSongName;
const songUrl = getQueryParam("songUrl") || defaultSongUrl;
const coverUrl = getQueryParam("coverUrl");
loadSong(songTitle, songUrl, coverUrl);