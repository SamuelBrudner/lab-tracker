---
name: scientific-visualization
description: Choose honest plot geometry for scientific data by first asking what the data's space actually contains — a meaningful origin (zero as absence, zero as neutral, or another reference such as chance), only differences, only directions or orientations, a wrapping axis, or only neighborhoods — and letting that drive mark type, axis limits, log scales, colormaps, aspect ratio, and scale bars versus ticks. Use whenever writing or reviewing code that plots experimental or analysis results (matplotlib, seaborn, ggplot2, MATLAB), critiquing a figure, or deciding bar vs. point plots, whether an axis should start at zero, sequential vs. diverging vs. cyclic colormaps, or how to show trajectories, headings, phases, orientations, images, or PCA/UMAP/t-SNE embeddings, even if the user never mentions origins. Complements general chart-styling and palette guidance; this skill covers what the plot's geometry claims.
---

# Scientific visualization: what the plot's geometry claims

Every plot makes geometric claims: that a bar's length means something, that the
point where the axes cross is special, that up differs from down, that angles and
distances between points are real. Misleading scientific figures usually get the
numbers right and the geometry wrong — they assert structure the data doesn't
have, or hide structure it does. So before picking marks, limits, or colormaps,
establish what the data's space contains.

## Step 1: Ask what the meaning is invariant to

Do this per encoding channel (x, y, color, size), and separately for any pair of
axes that share a unit (x–y position, two PC scores), because one plot often mixes
classes — time on x can be arbitrary while spike rate on y has a true zero.

1. **Shift:** would adding a constant to every value change the conclusion?
   No → there is no meaningful origin. Yes → there is one; is it a floor (nothing
   below it) or a neutral midpoint (signed values on both sides)?
2. **Reference:** is the value the claim is judged against actually zero? Chance
   performance, resting potential, and pre-stimulus baseline are origins too.
3. **Rotate / mirror / stretch** (axes sharing a unit): would rotating the plot,
   reflecting it, or stretching one axis change the meaning? If so, directions,
   handedness, or shape are part of the data and the plot must preserve them.
4. **Wrap:** is the maximum adjacent to the minimum (angle, phase, time of day)?
5. **Distance:** are distances meaningful at all, or only who is near whom?

The governing rule: **preserve in the plot exactly what the meaning is not
invariant to; suppress, or explicitly label as arbitrary, what it is invariant
to.** Drawing what the data lacks invites false readings; dropping what it has
hides the result.

State the classification in one line before writing the plotting code — a code
comment or a sentence in the response, e.g.
`# y: rate, zero = silence (absence); x: time re: odor onset (reference); color: heading (circular)`.
It makes the encoding choices reviewable and surfaces mismatches before they are
drawn.

## Step 2: Let the class drive the encoding

### A. Origin is absence (ratio scale)

Counts, concentration, firing rate, duration, area, distance from a source.

- Length and area encodings are valid — bars, stacked bars, filled areas, bubble
  area — and they require the value axis to include zero, because a bar's length
  *is* its claim. A truncated bar asserts a false ratio.
- Ratio language ("2× higher") and log scales are valid. Log needs strictly
  positive data; if zeros occur, use a labeled symlog/asinh scale rather than
  silently dropping them.
- Sequential colormap anchored at zero (`vmin=0`), not at the data minimum, so
  equal colors mean equal amounts across panels.
- In 2D, if a specific point is the origin (source, nest, fixation, target),
  center on it and mark it. When distance and bearing to it carry the claim,
  consider polar or radial coordinates.
- Histograms are fine even when x is arbitrary: bar length runs along the count
  axis, which has a true zero.

### B. Origin is a neutral midpoint (signed)

Change from baseline, ΔF/F, log₂ fold change, correlation, preference index,
signed velocity, z-scores (zero = sample mean).

- Always draw the zero reference line.
- Diverging colormap centered on zero with symmetric limits, so equal color steps
  mean equal magnitudes in both directions. Asymmetric two-slope norms make the
  two halves incomparable; use one only deliberately and say so.
- Deviation bars growing up and down from zero are valid; bars from the bottom
  of the axis are not.

### C. A meaningful reference that is not zero

Accuracy against chance (0.5 in 2AFC), membrane potential against rest or
reversal, behavior against a pre-stimulus baseline, a regulatory threshold.

- The claim is made against the reference, so draw it (line or band) and consider
  re-expressing the data relative to it, which moves it into class B.
- Bars from zero mislead here: 2AFC accuracy bars make chance look like half of
  perfect. Prefer points with a chance line, or deviation-from-chance bars.

### D. No meaningful origin, only differences (interval scale)

Clock or calendar time, °C, raw stage or pixel coordinates, position from an
arbitrary end of a track, acquisition timestamps.

- No length or area encodings: a bar drawn from an arbitrary baseline encodes
  only the choice of baseline. Use points, lines, intervals, and ranges.
- Don't force zero onto the axis; fit the limits to the data with a little
  padding. Including an arbitrary zero compresses the signal against nothing.
- No ratios, percent change, or log scales.
- Prefer re-referencing to an origin that carries the claim — time from stimulus
  onset, position relative to the source — and declare it in the axis label
  ("Time from odor onset (s)"). This is honest because the label names the
  chosen origin. When aligning to an event, show the pre-event window and mark
  t = 0.
- For spatial data, use a scale bar instead of ticked axes; tick values like
  "pixel 512" invite meaning to be read into arbitrary coordinates.

### E. Directions or orientations matter, positions don't

Physical frames with a meaningful axis (upwind–downwind, anterior–posterior,
dorsal–ventral, gravity), tracked trajectories, flow and vector fields, images.

- Lock the aspect ratio to 1:1 whenever two axes share a unit. Unequal scaling
  changes angles and shapes, which are exactly what is being shown. (When the
  axes have different units, aspect is a free design choice.)
- Canonicalize rotation to the meaningful direction (e.g., upwind always up),
  state the convention, and show a direction indicator (arrow, compass, "A ↔ P").
- Absolute positions are arbitrary: drop tick values and add a scale bar. To
  compare many trajectories, translate each to a common start and label it
  "start" — it is a chosen origin, so name it.
- Ask whether mirroring changes the meaning. If handedness matters (left vs.
  right turns, clockwise rotation, chirality), never flip an axis casually: image
  coordinates (y down; `imshow`'s default `origin="upper"`) and Cartesian
  coordinates (y up) give opposite signs for the same turn. Keep tracks and images
  in one stated convention. If the system is bilaterally symmetric and pooling
  mirrored data is legitimate, say so explicitly.

### F. Wrapping axes (circular data)

Heading, phase, orientation of an edge or limb, time of day, circadian phase.

- Distinguish direction (period 360°) from axial orientation (period 180°: a bar
  at 10° is the same as one at 190°).
- 0° and the positive sense are conventions; state them ("0° = upwind,
  clockwise positive").
- Use polar axes or rose histograms, or a linear axis that shows extra cycle
  (or cuts the circle far from where data cluster) so a cluster near the wrap
  point isn't split across both edges.
- Rose histograms: if wedge radius ∝ count, wedge area ∝ count², which
  exaggerates the largest bins. Scale radius by √count (area ∝ count) or label
  the choice.
- Use a cyclic colormap. A sequential map makes 359° and 1° look maximally
  different.
- Use circular statistics: the arithmetic mean of 359° and 1° is 180°. For axial
  data, double the angles, compute, then halve.

### G. Only neighborhoods matter (nonlinear embeddings)

UMAP, t-SNE, force-directed graph layouts.

- Origin, axis directions, rotation, global distances, and cluster sizes are not
  meaningful. Remove tick values and gridlines; use small labeled axis stubs
  ("UMAP 1", "UMAP 2") or nothing.
- Don't let the layout carry claims about inter-cluster distance, cluster area,
  or direction. Rotating or reflecting to match a previous figure is legitimate
  because it is free.

**PCA and other linear embeddings sit between E and G.** The origin is the data
mean (after centering) and axis order is meaningful (variance-ranked), but each
component's sign is arbitrary. Flip signs to a stated convention (e.g., PC1
positive with stimulus intensity) and keep it consistent across figures. Label
the variance explained on each axis, and since scores share units, use equal
aspect — a stretched PC2 looks as important as PC1.

## Summary

| The data has… | So the plot… |
|---|---|
| Zero = absence | may use bars/areas; value axis starts at 0; ratios and log OK; sequential colormap from 0 |
| Zero = neutral | zero line; diverging colormap centered on 0 with symmetric limits; deviation bars OK |
| A non-zero reference | draws the reference; prefer points or deviation-from-reference over bars from 0 |
| Only differences | points/lines/intervals; limits fit to data; no ratios or log; re-reference to a named event if possible |
| Meaningful directions | 1:1 aspect; canonical rotation; compass and scale bar instead of ticks; guard handedness |
| A wrapping axis | polar or wrapped axis; cyclic colormap; circular statistics; √count rose radii |
| Only neighborhoods | no tick values; no claims about distance, size, or direction |

## Red flags when reviewing a figure

- Bars or filled areas on interval data, or on a truncated value axis.
- Zero forced onto an interval axis, flattening the effect.
- Sequential colormap on signed data, or a diverging map not centered on the
  neutral value.
- Linear (non-cyclic) colormap or axis cut through a cluster of angles or phases.
- Unequal aspect on spatial, vector, or PCA plots.
- Tick values on UMAP/t-SNE axes or on arbitrary pixel coordinates.
- "% change" or "fold" applied to interval data.
- Event-aligned data with no marked t = 0 or no pre-event window.
- Trajectories mirrored between image and Cartesian conventions when turning
  direction is the result.
- Rose diagram with radius ∝ count.

## Implementation

For matplotlib/seaborn and ggplot2 idioms that implement each rule (zero-anchored
limits, centered norms, equal aspect, scale bars, polar axes, cyclic colormaps,
circular means), read `references/idioms.md` when writing the plotting code.
