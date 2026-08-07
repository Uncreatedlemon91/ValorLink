// Rich-text article editor (Quill, see static/vendor/quill/). The toolbar
// is deliberately narrow -- every option here maps to a tag/attribute
// proclubs/html_sanitize.py actually allows through. Anything Quill
// supports beyond that (color, font, alignment, embedded video) would
// just get silently stripped on save, so it's left off the toolbar
// rather than offered and then dropped.
(function () {
  const container = document.getElementById('editor');
  if (!container || typeof Quill === 'undefined') return;

  const toolbarOptions = [
    [{ header: [1, 2, 3, false] }],
    ['bold', 'italic', 'underline', 'strike'],
    ['blockquote', 'code-block'],
    [{ list: 'ordered' }, { list: 'bullet' }],
    ['link', 'image'],
    ['clean'],
  ];

  const quill = new Quill(container, {
    theme: 'snow',
    modules: { toolbar: toolbarOptions },
    placeholder: 'Write the article...',
  });

  // Quill's default image button prompts for a URL. Swap it for a file
  // picker that embeds the image as a data URI directly in the article
  // body -- same "no upload endpoint, just embed it" pattern already used
  // for the cover image and avatar fields elsewhere on this site.
  const MAX_IMAGE_BYTES = 3 * 1024 * 1024;
  quill.getModule('toolbar').addHandler('image', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.addEventListener('change', () => {
      const file = input.files[0];
      if (!file) return;
      if (file.size > MAX_IMAGE_BYTES) {
        alert('That image is too large (max 3MB). Try a smaller file.');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const range = quill.getSelection(true);
        quill.insertEmbed(range.index, 'image', reader.result, 'user');
        quill.setSelection(range.index + 1);
      };
      reader.readAsDataURL(file);
    });
    input.click();
  });

  const form = document.getElementById('article-form');
  const hiddenInput = document.getElementById('body_html_input');
  form.addEventListener('submit', () => {
    hiddenInput.value = quill.root.innerHTML;
  });
})();
