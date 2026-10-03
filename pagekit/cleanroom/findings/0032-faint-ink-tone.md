# Finding: faint iron-gall ink that needs more contrast in a grey image

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Faded iron-gall ink is brown or yellow-grey and low in contrast against yellowed paper. A plain luminance conversion can make it fainter still. The lead's document proposes a grey variant built from luminance or from the channel with the best ink contrast (it suggests that the blue channel or the minimum of the three channels may read best for iron-gall ink, to be tested), after flat-field correction, with contrast-limited local histogram equalisation and a gentle unsharp mask.

## Pagekit's observed behaviour

Not built yet. Pagekit reduces the master to grey for its own measurements; its limits note that faded ink can move to the paper side of its global threshold.

## General technique

Choose the grey conversion that maximises ink-to-paper contrast for the collection: compare luminance, single channels and channel minima on sample pages by the separation between ink and paper. Flatten illumination first. Then enhance local contrast with contrast-limited adaptive histogram equalisation, which equalises within tiles but limits the slope of the mapping so paper grain and noise are not blown up. A mild unsharp mask sharpens stroke edges. Measure every enhancement by recognition results, since enhancement can also amplify show-through and stains.
Source: K. Zuiderveld, "Contrast limited adaptive histogram equalization", Graphics Gems IV, Academic Press, 1994; general knowledge

## Settings in general terms

The channel choice depends on ink and paper colour and should be measured per collection. The equalisation tile size depends on letter and line size; the clip limit depends on paper noise. The unsharp mask radius depends on stroke width and its amount should stay small.
