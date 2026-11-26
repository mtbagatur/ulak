// The phone shows the hour it is where you are. On a wide screen the picture inside it becomes the demo itself:
// the real app over a pretend machine, in this tab.
(() => {
  const frame = document.querySelector('.phone[data-demo]');
  if (!frame) return;
  const clock = frame.querySelector('.bar b');
  const tick = () => { clock.textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}); };
  if (clock) { tick(); setInterval(tick, 20000); }
  if (!matchMedia('(min-width: 900px)').matches) return;
  const demo = document.createElement('iframe');
  demo.src = frame.dataset.demo;
  demo.title = frame.dataset.title;
  demo.addEventListener('load', () => frame.classList.add('live'), {once: true});
  frame.querySelector('.glass').append(demo);
})();
