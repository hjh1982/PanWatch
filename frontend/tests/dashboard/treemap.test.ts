import { describe, expect, it } from 'vitest'
import { squarify, squarifyGroups } from '@/lib/treemap'

const WIDTH = 600
const HEIGHT = 300

function area(x: { width: number; height: number }) {
  return x.width * x.height
}

describe('squarify', () => {
  it('returns nothing without usable entries or space', () => {
    expect(squarify([], WIDTH, HEIGHT)).toEqual([])
    expect(squarify([{ key: 'a', value: 1 }], 0, HEIGHT)).toEqual([])
    expect(squarify([{ key: 'a', value: 0 }], WIDTH, HEIGHT)).toEqual([])
  })

  it('fills the canvas proportionally to value', () => {
    const entries = [
      { key: 'a', value: 50 },
      { key: 'b', value: 30 },
      { key: 'c', value: 20 },
    ]
    const rects = squarify(entries, WIDTH, HEIGHT)

    expect(rects).toHaveLength(3)
    expect(rects.reduce((sum, rect) => sum + area(rect), 0)).toBeCloseTo(WIDTH * HEIGHT, 5)
    expect(area(rects[0]) / (WIDTH * HEIGHT)).toBeCloseTo(0.5, 5)
    expect(area(rects[1]) / (WIDTH * HEIGHT)).toBeCloseTo(0.3, 5)
    expect(area(rects[2]) / (WIDTH * HEIGHT)).toBeCloseTo(0.2, 5)
  })

  it('keeps every tile inside the canvas without overlaps', () => {
    const entries = Array.from({ length: 100 }, (_, i) => ({ key: `${i}`, value: 1000 - i * 7 }))
    const rects = squarify(entries, WIDTH, HEIGHT)

    expect(rects).toHaveLength(entries.length)
    for (const rect of rects) {
      expect(rect.x).toBeGreaterThanOrEqual(0)
      expect(rect.y).toBeGreaterThanOrEqual(0)
      expect(rect.x + rect.width).toBeLessThanOrEqual(WIDTH + 1e-6)
      expect(rect.y + rect.height).toBeLessThanOrEqual(HEIGHT + 1e-6)
    }

    for (let i = 0; i < rects.length; i += 1) {
      for (let j = i + 1; j < rects.length; j += 1) {
        const a = rects[i]
        const b = rects[j]
        const overlapWidth = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
        const overlapHeight = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
        expect(Math.max(0, overlapWidth) * Math.max(0, overlapHeight)).toBeCloseTo(0, 6)
      }
    }
  })

  it('sorts the largest value into the first tile regardless of input order', () => {
    const rects = squarify(
      [
        { key: 'small', value: 10 },
        { key: 'big', value: 90 },
        { key: 'mid', value: 40 },
      ],
      WIDTH,
      HEIGHT,
    )
    expect(rects[0].key).toBe('big')
    expect(area(rects[0])).toBeGreaterThan(area(rects[2]))
  })
})

describe('squarifyGroups', () => {
  const groups = [
    {
      key: '银行',
      value: 60,
      entries: [
        { key: 'a', value: 30 },
        { key: 'b', value: 20 },
        { key: 'c', value: 10 },
      ],
    },
    {
      key: '白酒',
      value: 40,
      entries: [
        { key: 'd', value: 25 },
        { key: 'e', value: 15 },
      ],
    },
  ]

  it('lays out groups and keeps every stock inside its industry band', () => {
    const { groups: groupRects, tiles } = squarifyGroups(groups, WIDTH, HEIGHT, { gap: 2, headerHeight: 15 })

    expect(groupRects).toHaveLength(2)
    expect(tiles).toHaveLength(5)
    for (const group of groupRects) {
      expect(group.headerHeight).toBeGreaterThan(0)
      for (const tile of tiles.filter((item) => item.group === group.key)) {
        expect(tile.x).toBeGreaterThanOrEqual(group.inner.x - 1e-6)
        expect(tile.y).toBeGreaterThanOrEqual(group.inner.y - 1e-6)
        expect(tile.x + tile.width).toBeLessThanOrEqual(group.inner.x + group.inner.width + 1e-6)
        expect(tile.y + tile.height).toBeLessThanOrEqual(group.inner.y + group.inner.height + 1e-6)
      }
    }
  })

  it('orders bands by total value and skips unusable groups', () => {
    const { groups: groupRects } = squarifyGroups(
      [...groups, { key: '空', value: 0, entries: [{ key: 'x', value: 1 }] }],
      WIDTH,
      HEIGHT,
    )

    expect(groupRects.map((group) => group.key)).toEqual(['银行', '白酒'])
    expect(squarifyGroups([], WIDTH, HEIGHT)).toEqual({ groups: [], tiles: [] })
    expect(squarifyGroups(groups, 0, HEIGHT)).toEqual({ groups: [], tiles: [] })
  })

  it('drops the band header when a group is too small to read', () => {
    const { groups: groupRects } = squarifyGroups(
      [
        { key: '大盘股', value: 90, entries: [{ key: 'big', value: 90 }] },
        { key: '小盘股', value: 10, entries: [{ key: 'tiny', value: 10 }] },
      ],
      400,
      200,
      { headerHeight: 15 },
    )

    expect(groupRects).toHaveLength(2)
    expect(groupRects[0].headerHeight).toBe(15)
    expect(groupRects[1].headerHeight).toBe(0)
  })
})