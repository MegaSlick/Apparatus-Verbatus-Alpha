# Finding: light writing on a dark ground, such as a negative microfilm frame

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Some frames are negatives: pale writing on a dark ground, as on negative microfilm. Every step that assumes dark ink on light paper (thresholds, shadow removal, content detection) fails on them. A naive test (are most pixels dark?) is fooled by a page surrounded by a large dark backdrop or border, which can make a normal page look mostly dark.

## Pagekit's observed behaviour

Not built yet. Pagekit separates ink and paper with a global threshold and trims nearly all-dark border rows and columns as backdrop; it has no polarity test, and a negative frame is not described in its specification.

## General technique

Decide polarity on the page area, not the frame. First test the share of dark pixels inside the page outline after a global threshold; paper is the majority class on a written page. When that test is ambiguous, restrict it to the region where writing is likely: places with strong local contrast in either direction (a white and a black top-hat both respond to strokes, whichever their polarity), grown into a content region, and repeat the majority test there. Invert negatives once at the start of analysis and record the flag, so output can be written in the polarity the readers expect.
Source: general knowledge

## Settings in general terms

The working resolution depends on stroke width. The size of the top-hat element depends on stroke width, and the growing of the content region depends on line spacing. The dark-share cut-off should be measured on both positive and negative frames from the collection, and an ambiguous result should be reviewed.
