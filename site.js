/* TripBee Pro support site — reveal animation and navigation */
(function () {
  'use strict';

  /* 捲動進場 */
  var els = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    els.forEach(function (el) { io.observe(el); });
  } else {
    els.forEach(function (el) { el.classList.add('in'); });
  }

  /* 導覽列捲動態 */
  var nav = document.querySelector('header.nav');
  function onScroll() { if (nav) nav.classList.toggle('scrolled', window.scrollY > 10); }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* 背景音樂（與 App 相同 BGM）。瀏覽器禁止自動播放，一律由使用者點擊開啟。 */
  var btn = document.getElementById('musicBtn');
  if (!btn) return;
  var audio = new Audio('assets/bgm.mp3');
  audio.loop = true;
  audio.volume = 0.32;
  audio.preload = 'none';
  var playing = false;

  function render() {
    btn.classList.toggle('playing', playing);
    btn.setAttribute('aria-pressed', playing ? 'true' : 'false');
    btn.setAttribute('aria-label', playing ? 'Pause background music' : 'Play background music');
  }

  btn.addEventListener('click', function () {
    if (playing) { audio.pause(); playing = false; render(); return; }
    audio.play().then(function () { playing = true; render(); })
      .catch(function () { playing = false; render(); });
  });
  render();
})();
