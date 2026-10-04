# Finding: declared coordinate spaces and one pixel-edge convention

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A page passes through several grids: the stored source, the upright source, the selected page, the prepared page and the screen preview. Annotations, protected marks and corrections must stay attached to the same writing when any step changes. Libraries differ on whether a pixel's coordinate names its corner or its centre, which can shift results by half a pixel.

## Pagekit's observed behaviour

Already: coordinates are continuous, a pixel covering the unit square from its index; the manifest stores the composed affine maps both ways; points and polygons map in both directions and return within a fraction of a pixel; cuts are given in the upright frame and boxes in the levelled page's grid.

Differently: corrections are stored in several grids (upright frame for the cut, levelled page for boxes) rather than all in source coordinates, which is why a manual value is flagged when an earlier step changes.

Not yet: named coordinate spaces for each grid including the display preview; annotations persisted in source coordinates; browser pointer positions treated only as temporary.

## General technique

Name each coordinate space and give every stored point its space. Store durable annotations in the source space, and derive every other space from them through the chain's maps. Fix one convention (pixel edges at integers, centres at half-integers) and write explicit conversions wherever a library uses the other.
Source: Heckbert, What are the coordinates of a pixel?, in Graphics Gems, Academic Press, 1990

## Settings in general terms

- None numeric. The convention and the list of spaces are fixed and documented once.
