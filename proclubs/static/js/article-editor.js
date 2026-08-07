// Rich-text article editor (Quill, see static/vendor/quill/). The toolbar
// is deliberately narrow -- every option here maps to a tag/attribute
// proclubs/html_sanitize.py actually allows through. Anything Quill
// supports beyond that (color, font, alignment, embedded video) would
// just get silently stripped on save, so it's left off the toolbar
// rather than offered and then dropped.
(function () {
  const container = document.getElementById('editor');
  if (!container || typeof Quill === 'undefined') return;

  // A placeholder for a Discord clip (see services.render_clip_embeds).
  // Registered as a real Quill blot -- not just inserted as raw HTML --
  // so it round-trips correctly: parses back into an embed when editing
  // an existing article, and serializes back out as <clip-embed> on save.
  // The label is a save-time snapshot for display in THIS editor only; the
  // public article page never reads it, it always resolves the id fresh.
  const BlockEmbed = Quill.import('blots/block/embed');
  class ClipEmbed extends BlockEmbed {
    static create(value) {
      const node = super.create();
      node.setAttribute('data-clip-id', value.id);
      node.setAttribute('contenteditable', 'false');
      node.textContent = value.label || ('Clip #' + value.id);
      return node;
    }
    static value(node) {
      return { id: node.getAttribute('data-clip-id'), label: node.textContent };
    }
  }
  ClipEmbed.blotName = 'clipEmbed';
  ClipEmbed.tagName = 'clip-embed';
  Quill.register(ClipEmbed);

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

  // "Insert Clip" -- lists recently-synced Discord clips (GET /api/clips)
  // and drops the chosen one in as a clipEmbed at the cursor.
  const insertClipBtn = document.getElementById('insert-clip-btn');
  const clipPanel = document.getElementById('clip-picker-panel');
  if (insertClipBtn && clipPanel) {
    let loaded = false;
    let savedRange = null;

    function closePanel() {
      clipPanel.hidden = true;
    }

    function renderClips(clips) {
      clipPanel.innerHTML = '';
      if (!clips.length) {
        clipPanel.innerHTML = '<p class="clip-picker-empty">No clips synced yet -- post a video in the clips channel on Discord.</p>';
        return;
      }
      clips.forEach((clip) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'clip-picker-row';
        const label = clip.title || clip.filename || ('Clip from ' + (clip.authorName || 'Discord'));
        const when = new Date(clip.postedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
        row.innerHTML = '<span class="clip-picker-row-title"></span><span class="clip-picker-row-meta"></span>';
        row.querySelector('.clip-picker-row-title').textContent = label;
        row.querySelector('.clip-picker-row-meta').textContent = (clip.authorName ? clip.authorName + ' · ' : '') + when;
        row.addEventListener('click', () => {
          const index = savedRange ? savedRange.index : quill.getLength();
          quill.insertEmbed(index, 'clipEmbed', { id: clip.id, label: '🎥 ' + label }, 'user');
          quill.setSelection(index + 1);
          closePanel();
        });
        clipPanel.appendChild(row);
      });
    }

    insertClipBtn.addEventListener('click', () => {
      if (!clipPanel.hidden) {
        closePanel();
        return;
      }
      savedRange = quill.getSelection(true);
      clipPanel.hidden = false;
      if (loaded) return;
      clipPanel.innerHTML = '<p class="clip-picker-empty">Loading clips&hellip;</p>';
      fetch('/api/clips')
        .then((r) => {
          if (!r.ok) throw new Error('failed to load clips');
          return r.json();
        })
        .then((clips) => {
          loaded = true;
          renderClips(clips);
        })
        .catch(() => {
          clipPanel.innerHTML = '<p class="clip-picker-empty">Couldn’t load clips right now.</p>';
        });
    });

    document.addEventListener('click', (e) => {
      if (!clipPanel.hidden && !clipPanel.contains(e.target) && e.target !== insertClipBtn) {
        closePanel();
      }
    });
  }

  const form = document.getElementById('article-form');
  const hiddenInput = document.getElementById('body_html_input');
  form.addEventListener('submit', () => {
    hiddenInput.value = quill.root.innerHTML;
  });
})();
