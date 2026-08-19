# Design QA report

The conversation controls and knowledge-base settings match the existing
desktop design language. The final comparison has no actionable P0, P1, or P2
findings.

## Evidence

This QA pass uses the following visual evidence:

- Source visual truth: the user-provided Harness desktop screenshot and
  `reference-chat-1280x720.png`.
- Default-state implementation:
  `implementation-chat-conversation-controls-1280x720.png`.
- Delete-hover implementation:
  `implementation-chat-delete-hover-1280x720.png`.
- Knowledge settings implementation:
  `implementation-knowledge-switches-1280x720.png`.
- Combined comparison: `design-qa-conversation-controls.png`.
- CSS viewport: 1280 x 720.
- Source pixels: 1280 x 720.
- Implementation pixels: 1280 x 720.
- Browser device pixel ratio: 2. The capture surface normalized screenshots to
  CSS-pixel dimensions, so no additional resampling was required.
- State: desktop light theme with an active historical conversation.

The combined full-view comparison places the source, default implementation,
and delete-hover implementation in one image. It verifies the three-column
composition, 20 px canvas spacing, panel radii, navigation density, active-row
styling, composer placement, and context-card hierarchy.

The hover capture is the focused comparison for the new delete affordance. The
knowledge-page capture is the focused comparison for the global enable switch.
Both controls remain readable at the target viewport without changing the
existing icon or token system.

## Findings

The final pass has no actionable P0, P1, or P2 findings.

- Fonts and typography: Chinese system fallbacks preserve the existing size,
  weight, line height, truncation, and hierarchy. Session titles are stronger
  than their secondary timestamps.
- Spacing and layout rhythm: the recent-session rows remain compact, the
  delete action fits the existing active-row geometry, and removing the
  knowledge selector gives the composer balanced vertical padding.
- Colors and visual tokens: the implementation reuses the existing blue active
  state, neutral metadata, soft white surfaces, red destructive state, and
  green enabled state.
- Image and icon fidelity: the changed controls require no raster assets.
  Existing Material Symbols provide the chat, delete, database, and upload
  icons.
- Copy and content: the conversation title comes from the first user message,
  delete confirmation is explicit, and knowledge-base labels clearly state
  whether retrieval is enabled.

## Comparison history

The first pass found two P2 issues:

- The knowledge page emitted an Ant Design deprecation warning when its modal
  mounted.
- Existing historical sessions with stored messages could still display the
  default title until another message was sent.

The implementation now uses `destroyOnHidden`, derives display titles for
existing sessions from their first user message, and persists titles when the
first new message is sent. The final browser pass has no console errors. The
remaining console output consists only of React Router v7 future-flag warnings.

## Interaction and runtime verification

The browser pass verified the following interactions:

- The delete action appears on row hover and opens a two-step confirmation.
- Canceling the confirmation preserves the conversation.
- A knowledge base can be disabled and re-enabled from its management card.
- The chat composer no longer displays a per-message knowledge selector.
- The in-app preview remains available at the local development URL.

The frontend production build passed. The complete backend suite passed with 45
tests, including session-title generation, knowledge-base status updates, and
disabled knowledge-base retrieval exclusion.

## Final result

final result: passed
