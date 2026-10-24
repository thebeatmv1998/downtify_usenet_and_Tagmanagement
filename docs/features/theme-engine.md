# Theme Engine

**Settings → General → Theme engine** changes the look of the web UI for every
account and device on this server. It is an admin setting, stored in
`settings.json` like the rest of the server options.

## Colours

| Field | What it changes |
|-------|-----------------|
| **Text colour** | Body text and the derived secondary text shades |
| **Primary colour** | Buttons, links, the active tab, focus rings and progress bars |
| **Secondary colour** | The hover/highlight variant of the primary colour |
| **Background colour** | The page background; panels, surfaces and borders are derived from it and the text colour |

Leave a field empty to keep the design's own value. A colour picker and a text
field are shown for each one; **Reset theme** clears all six fields, including
the custom CSS.

The values are injected as CSS variables, so they work in both the dark and the
light mode. If only the primary colour is set, the hover colour and the
foreground on filled buttons are derived automatically (bright accents get dark
text, dark accents get light text).

## Font

**Font family** takes any CSS font stack, for example:

```
'Sora Variable', 'Segoe UI', sans-serif
```

Downtify bundles **Sora** and **DM Sans** (the design fonts). Any other family
must be installed on the viewer's device, or loaded through custom CSS with an
`@font-face`/`@import` rule. The chosen stack is used for both body text and
display headings.

## Custom CSS

The **Custom CSS** box is appended after the design tokens, so any rule you
write wins. Common starting points:

```css
/* the design variables (see frontend/src/styles/tokens.css) */
:root {
  --c-accent: #ff7a00;
  --radius-panel: 12px;
}

/* a specific screen */
.rounded-panel { box-shadow: none; }
```

It is inserted as a CSS text node, never as HTML, so `<script>` inside it does
nothing. The server validates colours and the font list and caps the CSS at
20,000 characters; invalid colours or font strings are dropped silently rather
than saved.

## Live preview and saving

Changes preview immediately in the settings page. **Save changes** stores them
for everyone; leaving the page without saving restores the saved theme. A
non-admin account does not see the panel, but does receive the resulting theme.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/theme` | The public theme (colours, font, custom CSS). No session required, so the sign-in page is themed too. |

The theme is also part of `GET /api/settings` (admin) and is written back with
`POST /api/settings/update` like every other setting.
