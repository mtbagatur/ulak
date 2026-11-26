// On a wide screen the picture in the phone frame becomes the demo itself: the real app over a pretend machine, in this tab.
(() => {
  const frame = document.querySelector('.phone[data-demo]');
  if (!frame || !matchMedia('(min-width: 900px)').matches) return;
  const demo = document.createElement('iframe');
  demo.src = frame.dataset.demo;
  demo.title = frame.dataset.title;
  demo.addEventListener('load', () => frame.classList.add('live'), {once: true});
  frame.append(demo);
})();
