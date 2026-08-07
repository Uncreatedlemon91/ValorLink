// Cover image focal-point picker (see news_form.html): click the preview
// to set which part of the image stays visible when it's cropped narrower
// elsewhere on the site (home hero, article header, card thumbnails).
document.addEventListener('DOMContentLoaded', () => {
  const picker = document.getElementById('focal-picker');
  const preview = document.getElementById('focal-preview');
  const marker = document.getElementById('focal-marker');
  const fileInput = document.getElementById('cover_image');
  const xInput = document.getElementById('cover_focal_x');
  const yInput = document.getElementById('cover_focal_y');

  if (!picker || !preview || !marker || !fileInput || !xInput || !yInput) return;

  function setFocal(xPct, yPct) {
    xInput.value = xPct.toFixed(1);
    yInput.value = yPct.toFixed(1);
    marker.style.left = xPct + '%';
    marker.style.top = yPct + '%';
    preview.style.objectPosition = xPct + '% ' + yPct + '%';
  }

  picker.addEventListener('click', (event) => {
    const rect = picker.getBoundingClientRect();
    const xPct = Math.min(100, Math.max(0, ((event.clientX - rect.left) / rect.width) * 100));
    const yPct = Math.min(100, Math.max(0, ((event.clientY - rect.top) / rect.height) * 100));
    setFocal(xPct, yPct);
  });

  // A newly-chosen file replaces the preview so the focal point is picked
  // against the actual new image, not whatever was there before -- and
  // resets to center, since the old coordinates likely don't apply to a
  // differently-framed photo.
  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      preview.src = event.target.result;
      picker.hidden = false;
      setFocal(50, 50);
    };
    reader.readAsDataURL(file);
  });
});
