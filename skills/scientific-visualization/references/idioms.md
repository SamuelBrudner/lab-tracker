# Idioms: implementing the geometry rules

Python assumes:

```python
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.colors import CenteredNorm
from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar
from scipy.stats import circmean
```

R assumes `library(ggplot2)`; `scico` and `circular` are named where used.

## A. Origin is absence

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| Value axis includes 0 | `ax.set_ylim(bottom=0)` (`sns.barplot` already does) | `expand_limits(y = 0)`, or `scale_y_continuous(limits = c(0, NA), expand = expansion(mult = c(0, 0.05)))` |
| Sequential colormap from 0 | `ax.imshow(img, cmap="viridis", vmin=0)` | `scale_fill_viridis_c(limits = c(0, NA))` |
| Log axis (positive data) | `ax.set_yscale("log")` | `scale_y_log10()` |
| Log-like axis with zeros | `ax.set_yscale("asinh")` or `ax.set_yscale("symlog", linthresh=t)`; label it | `scale_y_continuous(transform = "pseudo_log")` (`trans =` before ggplot2 3.5) |

## B. Origin is a neutral midpoint

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| Zero reference line | `ax.axhline(0, color="0.5", lw=0.8, zorder=0)` | `geom_hline(yintercept = 0, colour = "grey50")` |
| Diverging, centered, symmetric | `ax.imshow(d, cmap="RdBu_r", norm=CenteredNorm(vcenter=0))`; `sns.heatmap(df, cmap="vlag", center=0)` | `scale_fill_gradient2(midpoint = 0)`, or `scale_fill_distiller(palette = "RdBu", limits = c(-m, m))` with `m <- max(abs(x))` |

Avoid `TwoSlopeNorm` with unequal `vmin`/`vmax` unless the asymmetry is intended
and labeled: it gives the two halves different color-per-unit slopes.

## C. A non-zero reference

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| Draw the reference | `ax.axhline(0.5, ls="--", label="chance")` | `geom_hline(yintercept = 0.5, linetype = "dashed")` |
| Deviation from reference | `ax.bar(x, acc - 0.5, bottom=0.5)` | `geom_linerange(aes(ymin = 0.5, ymax = acc))` |

## D. No meaningful origin

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| Points, not bars | `sns.pointplot`, `sns.stripplot`, `ax.errorbar(x, y, yerr=e, fmt="o")` | `geom_pointrange()`, `geom_point()` |
| Limits fit to data | the default; just don't call `set_ylim(0, …)` | the default; no `expand_limits(y = 0)` |
| Mark the chosen origin | `ax.axvline(0, ls="--")`; `ax.set_xlabel("Time from odor onset (s)")` | `geom_vline(xintercept = 0, linetype = "dashed")` + `labs(x = "Time from odor onset (s)")` |

## E. Directions matter, positions don't

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| 1:1 aspect | `ax.set_aspect("equal")` | `coord_fixed()` |
| Scale bar, no ticks | `ax.add_artist(AnchoredSizeBar(ax.transData, 100, "100 µm", loc="lower right", frameon=False))`; `ax.set(xticks=[], yticks=[])` | `annotate("segment", x = x0, xend = x0 + 100, y = y0, yend = y0)` + `annotate("text", …)`; `theme(axis.text = element_blank(), axis.ticks = element_blank())` |
| Direction indicator | `ax.annotate("upwind", xy=(0.92, 0.95), xytext=(0.92, 0.78), xycoords="axes fraction", arrowprops={"arrowstyle": "->"}, ha="center")` | `annotate("segment", …, arrow = arrow(length = unit(2, "mm")))` |

Canonical rotation (put a direction at angle `phi` pointing up):

```python
def rotate(xy: np.ndarray, theta: float) -> np.ndarray:
    """Rotate (n, 2) points counterclockwise by ``theta`` radians about the origin."""
    c, s = np.cos(theta), np.sin(theta)
    return xy @ np.array([[c, s], [-s, c]])

upwind_up = rotate(xy - xy[0], np.pi / 2 - phi)  # also translates to a "start" origin
```

Handedness: `imshow` defaults to `origin="upper"` (y grows downward). Plot pixel
tracks on the same axes as the image and they line up; before computing turning
angles in a Cartesian frame, convert `y_cart = -y_px` so left and right keep
their meaning.

## F. Wrapping axes

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| Polar, 0° up, clockwise | `ax = fig.add_subplot(projection="polar")`; `ax.set_theta_zero_location("N")`; `ax.set_theta_direction(-1)` | `coord_polar(start = 0, direction = 1)` |
| Rose with area ∝ count | `ax.bar(edges[:-1], np.sqrt(counts), width=np.diff(edges), align="edge")`; label the radius "√count" | `geom_col(aes(y = n))` + `scale_y_sqrt()` + `coord_polar()` |
| Cyclic colormap | `cmap="twilight"`, `vmin=-np.pi, vmax=np.pi` | `scico::scale_colour_scico(palette = "romaO", limits = c(-pi, pi))` |
| Wrap to (−π, π] | `np.angle(np.exp(1j * theta))` | `atan2(sin(theta), cos(theta))` |
| Show the wrap on a linear axis | plot `np.r_[t, t + 2 * np.pi]` (repeat shaded) with `ax.set_xlim(-np.pi, 2 * np.pi)` | `rbind(d, transform(d, theta = theta + 2 * pi))` |
| Circular mean / concentration | `circmean(theta, high=np.pi, low=-np.pi)`; `np.abs(np.mean(np.exp(1j * theta)))` | `circular::mean.circular(circular::circular(theta))`; `circular::rho.circular(...)` |
| Axial (180°-periodic) mean | `circmean(2 * theta, high=np.pi, low=-np.pi) / 2` | same doubling, then halve |

## G. Embeddings

| Rule | matplotlib / seaborn | ggplot2 |
|---|---|---|
| UMAP/t-SNE: no tick values | `ax.set(xticks=[], yticks=[], xlabel="UMAP 1", ylabel="UMAP 2")` | `labs(x = "UMAP 1", y = "UMAP 2")` + `theme(axis.text = element_blank(), axis.ticks = element_blank(), panel.grid = element_blank())` |
| PCA: variance in labels, equal aspect | `ax.set_xlabel(f"PC1 ({pca.explained_variance_ratio_[0]:.0%} var.)")`; `ax.set_aspect("equal")` | `labs(x = sprintf("PC1 (%.0f%% var.)", 100 * ve[1]))` + `coord_fixed()` |

PCA sign convention (scikit-learn shapes), applied to scores and loadings together:

```python
sign = np.sign(np.corrcoef(scores[:, 0], intensity)[0, 1])  # PC1 positive with intensity
scores[:, 0] *= sign
pca.components_[0] *= sign
```
