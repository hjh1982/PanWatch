export interface TreemapEntry {
  key: string
  value: number
}

export interface TreemapRect {
  key: string
  x: number
  y: number
  width: number
  height: number
}

/**
 * Squarified treemap layout (Bruls et al. 2000).
 * Area is proportional to value while keeping tiles as square as possible.
 */
export function squarify(entries: TreemapEntry[], width: number, height: number): TreemapRect[] {
  const usable = entries.filter((entry) => Number.isFinite(entry.value) && entry.value > 0)
  if (usable.length === 0 || width <= 0 || height <= 0) return []

  const total = usable.reduce((sum, entry) => sum + entry.value, 0)
  const areas = usable
    .slice()
    .sort((a, b) => b.value - a.value)
    .map((entry) => ({ key: entry.key, area: (entry.value / total) * width * height }))

  const rects: TreemapRect[] = []
  let x = 0
  let y = 0
  let remainingWidth = width
  let remainingHeight = height
  let index = 0

  while (index < areas.length && remainingWidth > 0 && remainingHeight > 0) {
    const side = Math.min(remainingWidth, remainingHeight)
    let end = index + 1
    let sum = areas[index].area
    let max = areas[index].area
    let min = areas[index].area

    while (end < areas.length) {
      const area = areas[end].area
      const nextSum = sum + area
      const nextMax = Math.max(max, area)
      const nextMin = Math.min(min, area)
      if (worstRatio(nextSum, nextMax, nextMin, side) > worstRatio(sum, max, min, side)) break
      sum = nextSum
      max = nextMax
      min = nextMin
      end += 1
    }

    if (remainingWidth >= remainingHeight) {
      const stripWidth = sum / remainingHeight
      let offset = y
      for (let i = index; i < end; i += 1) {
        const tileHeight = areas[i].area / stripWidth
        rects.push({ key: areas[i].key, x, y: offset, width: stripWidth, height: tileHeight })
        offset += tileHeight
      }
      x += stripWidth
      remainingWidth -= stripWidth
    } else {
      const stripHeight = sum / remainingWidth
      let offset = x
      for (let i = index; i < end; i += 1) {
        const tileWidth = areas[i].area / stripHeight
        rects.push({ key: areas[i].key, x: offset, y, width: tileWidth, height: stripHeight })
        offset += tileWidth
      }
      y += stripHeight
      remainingHeight -= stripHeight
    }

    index = end
  }

  return rects
}

export interface TreemapGroupInput extends TreemapEntry {
  entries: TreemapEntry[]
}

export interface TreemapGroupRect extends TreemapRect {
  headerHeight: number
  inner: TreemapRect
}

export interface GroupedTreemap {
  groups: TreemapGroupRect[]
  tiles: (TreemapRect & { group: string })[]
}

export interface GroupLayoutOptions {
  /** 组与组之间的留白(像素)。 */
  gap?: number
  /** 组标题条高度;过高或过窄的组会自动省略标题。 */
  headerHeight?: number
}

/**
 * 两级云图:先按组(行业)面积铺满画布,再在每组标题条下方铺个股。
 * 组标题条只在空间足够时出现,避免小板块被标题吃掉。
 */
export function squarifyGroups(
  groups: TreemapGroupInput[],
  width: number,
  height: number,
  options: GroupLayoutOptions = {},
): GroupedTreemap {
  const gap = Math.max(0, options.gap ?? 3)
  const headerHeight = Math.max(0, options.headerHeight ?? 16)
  const usable = groups.filter(
    (group) => Number.isFinite(group.value) && group.value > 0 && group.entries.length > 0,
  )
  if (usable.length === 0 || width <= 0 || height <= 0) return { groups: [], tiles: [] }

  const byKey = new Map(usable.map((group) => [group.key, group]))
  const groupRects: TreemapGroupRect[] = []
  const tiles: (TreemapRect & { group: string })[] = []
  const half = gap / 2

  for (const rect of squarify(usable, width, height)) {
    const group = byKey.get(rect.key)
    if (!group) continue
    const x = rect.x + half
    const y = rect.y + half
    const groupWidth = rect.width - gap
    const groupHeight = rect.height - gap
    if (groupWidth <= 0 || groupHeight <= 0) continue

    const hasHeader = groupWidth >= 52 && groupHeight >= 30
    const head = hasHeader ? Math.min(headerHeight, groupHeight * 0.4) : 0
    const inner: TreemapRect = {
      key: rect.key,
      x,
      y: y + head,
      width: groupWidth,
      height: groupHeight - head,
    }
    groupRects.push({ key: rect.key, x, y, width: groupWidth, height: groupHeight, headerHeight: head, inner })
    if (inner.width <= 0 || inner.height <= 0) continue

    for (const tile of squarify(group.entries, inner.width, inner.height)) {
      tiles.push({ ...tile, x: tile.x + inner.x, y: tile.y + inner.y, group: rect.key })
    }
  }

  return { groups: groupRects, tiles }
}

function worstRatio(sum: number, max: number, min: number, side: number): number {
  if (sum <= 0 || side <= 0 || min <= 0) return Infinity
  const sideSquared = side * side
  const sumSquared = sum * sum
  return Math.max((sideSquared * max) / sumSquared, sumSquared / (sideSquared * min))
}