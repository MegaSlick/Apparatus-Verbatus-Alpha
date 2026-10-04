# Finding: colour files whose channels are equal, or nearly equal

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A capture stored as a colour file may have identical red, green and blue values at every pixel, so it holds only grey. Another may have channels that differ by a few levels, which may be noise or may be faint real colour. The file's colour mode alone does not say which.

## Pagekit's observed behaviour

Already: a palette image whose colours are all grey is written as grey.

Differently: an ordinary colour file is always kept as colour, even when its three channels are equal everywhere.

Not yet: a check that the channels are exactly equal; a path that takes the common channel and writes it as grey with the record saying the values were carried over unchanged; a distinction between exactly equal and nearly equal channels.

## General technique

When every decoded pixel has equal channels, taking any one channel loses no numeric value: the grey page holds exactly the intensities the file held, under a neutral interpretation. This is lossless and can be stated as such. When the channels are only nearly equal, any rule (luminance, average, one channel) changes values slightly, and the small differences do not on their own prove the colour carried nothing; that case is a reviewed conversion, not a lossless one. The test for exact equality is a simple full comparison of the decoded channels.
Source: general knowledge

## Settings in general terms

- Exact equality: no tolerance; it holds or it does not, over every decoded pixel.
- Near equality: a tolerance that should depend on the measured channel noise of the capture, used only to suggest that review is likely to be quick, never to declare the conversion lossless.
