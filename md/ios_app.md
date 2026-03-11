# GMX Trading Bot iOS App — Complete Specification

> **Purpose**: This document provides 100% of the information needed to build the iOS app. Follow it exactly.

---

## Table of Contents

1. [App Overview](#app-overview)
2. [Tech Stack](#tech-stack)
3. [Navigation Structure](#navigation-structure)
4. [Theme System](#theme-system)
5. [Screen Specifications](#screen-specifications)
   - [Home Tab](#home-tab)
   - [History Tab](#history-tab)
   - [Notifications Tab](#notifications-tab)
   - [Settings Tab](#settings-tab)
6. [Data Models](#data-models)
7. [API Endpoints](#api-endpoints)
8. [Components Library](#components-library)
9. [Animations & Interactions](#animations--interactions)
10. [Typography & Spacing](#typography--spacing)
11. [Icons](#icons)

---

## App Overview

A mobile dashboard for monitoring a GMX perpetual trading bot running on Arbitrum. The app is **read-only** for trade execution (the bot handles entries/exits automatically) but provides:

- Real-time wallet balance with historical charts
- Live active positions with PnL updates
- Trade history with expandable detail cards
- Push notifications for trade events
- Customizable themes (all dark/black variants)

---

## Tech Stack

### iOS App
| Component | Technology |
|-----------|------------|
| Framework | SwiftUI (iOS 17+) |
| Architecture | MVVM with @Observable |
| Networking | URLSession + async/await |
| WebSocket | URLSessionWebSocketTask |
| Charts | Swift Charts |
| Push Notifications | APNs |
| Local Storage | SwiftData or UserDefaults |
| Auth | Keychain for tokens |

### Backend (VPS)
| Component | Technology |
|-----------|------------|
| API | FastAPI (Python) |
| Database | PostgreSQL |
| Cache | Redis |
| WebSocket | FastAPI WebSockets |
| Push Service | APNs via PyJWT + httpx |

---

## Navigation Structure

```
┌─────────────────────────────────────────────────┐
│                   Tab Bar                        │
├───────────┬───────────┬───────────┬─────────────┤
│   Home    │  History  │  Alerts   │  Settings   │
│  (house)  │  (clock)  │  (bell)   │   (gear)    │
└───────────┴───────────┴───────────┴─────────────┘
```

### Tab Bar Configuration

| Index | Tab | Icon | Label | Badge |
|-------|-----|------|-------|-------|
| 0 | Home | `house.fill` | Home | None |
| 1 | History | `clock.arrow.circlepath` | History | None |
| 2 | Notifications | `bell.fill` | Alerts | Unread count (red circle) |
| 3 | Settings | `gearshape.fill` | Settings | None |

**Tab Bar Styling:**
- Background: `theme.card` color
- Border top: 1px `theme.border` color
- Height: 83px (includes home indicator safe area)
- Active icon: `theme.accentText` color
- Inactive icon: `theme.textMuted` color
- Label font: 10px semibold

---

## Theme System

All themes are dark/black variants optimized for OLED displays.

### Theme Definition Schema

```swift
struct AppTheme {
    let name: String
    let description: String
    let bg: Color           // Main background
    let card: Color         // Card/surface background
    let cardHover: Color    // Pressed/hover state
    let accentBg: Color     // Primary accent (solid)
    let accentText: Color   // Primary accent (text)
    let accentBgSoft: Color // Accent at 20% opacity
    let text: Color         // Primary text
    let textMuted: Color    // Secondary text
    let border: Color       // Borders and dividers
    let chartColor: Color   // Chart line color
}
```

### Theme Definitions

#### 1. Midnight (Default)
```
name: "Midnight"
description: "Classic dark"
bg: #000000
card: #111111 (gray-900)
cardHover: #1F1F1F (gray-800)
accentBg: #22C55E (green-500)
accentText: #4ADE80 (green-400)
accentBgSoft: rgba(34, 197, 94, 0.2)
text: #FFFFFF
textMuted: #9CA3AF (gray-400)
border: #1F1F1F (gray-800)
chartColor: #22C55E
```

#### 2. AMOLED Black
```
name: "AMOLED Black"
description: "Pure black, saves battery"
bg: #000000
card: #0A0A0A (zinc-950)
cardHover: #18181B (zinc-900)
accentBg: #FFFFFF
accentText: #FFFFFF
accentBgSoft: rgba(255, 255, 255, 0.1)
text: #FFFFFF
textMuted: #71717A (zinc-500)
border: #18181B (zinc-900)
chartColor: #FFFFFF
```

#### 3. Matrix
```
name: "Matrix"
description: "Cyberpunk green"
bg: #000000
card: #030712 (gray-950)
cardHover: #111827 (gray-900)
accentBg: #10B981 (emerald-500)
accentText: #34D399 (emerald-400)
accentBgSoft: rgba(16, 185, 129, 0.2)
text: #ECFDF5 (emerald-50)
textMuted: #047857 (emerald-700)
border: rgba(6, 78, 59, 0.3) (emerald-900/30)
chartColor: #10B981
```

#### 4. Stealth
```
name: "Stealth"
description: "Dark gray minimal"
bg: #0A0A0A (neutral-950)
card: #171717 (neutral-900)
cardHover: #262626 (neutral-800)
accentBg: #F5F5F5 (neutral-100)
accentText: #D4D4D4 (neutral-300)
accentBgSoft: #404040 (neutral-700)
text: #FFFFFF
textMuted: #737373 (neutral-500)
border: #262626 (neutral-800)
chartColor: #A3A3A3
```

#### 5. Ocean
```
name: "Ocean"
description: "Deep blue dark"
bg: #020617 (slate-950)
card: #0F172A (slate-900)
cardHover: #1E293B (slate-800)
accentBg: #06B6D4 (cyan-500)
accentText: #22D3EE (cyan-400)
accentBgSoft: rgba(6, 182, 212, 0.2)
text: #FFFFFF
textMuted: #94A3B8 (slate-400)
border: #1E293B (slate-800)
chartColor: #06B6D4
```

#### 6. Ember
```
name: "Ember"
description: "Dark with orange accents"
bg: #0C0A09 (stone-950)
card: #1C1917 (stone-900)
cardHover: #292524 (stone-800)
accentBg: #F97316 (orange-500)
accentText: #FB923C (orange-400)
accentBgSoft: rgba(249, 115, 22, 0.2)
text: #FFFFFF
textMuted: #A8A29E (stone-400)
border: #292524 (stone-800)
chartColor: #F97316
```

### Theme Storage

Store selected theme ID in UserDefaults:
```swift
@AppStorage("selectedTheme") var selectedTheme: String = "midnight"
```

---

## Screen Specifications

### Home Tab

The main dashboard showing wallet balance, performance chart, and active positions.

#### Layout Structure

```
┌────────────────────────────────────────┐
│ [Status Bar - 44pt]                    │
├────────────────────────────────────────┤
│                                        │
│  Total Balance            [top: 24px]  │
│  $14,823.45               [32pt bold]  │
│  ↑ +$342.18 (2.36%) today [14pt]      │
│                                        │
├────────────────────────────────────────┤
│                                        │
│  ┌────────────────────────────────┐   │
│  │      Area Chart (176pt)        │   │
│  │      (gradient fill)           │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│  [Today] [30 Days] [All Time]         │
│  (pill buttons, centered)              │
├────────────────────────────────────────┤
│                                        │
│  Active Positions          ⟳ Live     │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ [E] ETH    LONG 10x   +$168.25│   │
│  │     2.5 ETH           +2.07%  │   │
│  │     Entry $3,245  Now $3,312  │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ [B] BTC    SHORT 5x    +$66.00│   │
│  │     0.15 BTC          +1.50%  │   │
│  │     Entry $67,890 Now $67,450 │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ [A] ARB    LONG 3x    -$60.00 │   │
│  │     1500 ARB          -3.57%  │   │
│  │     Entry $1.12   Now $1.08   │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│ [Home] [History] [Alerts] [Settings]  │
└────────────────────────────────────────┘
```

#### Balance Header

| Element | Style |
|---------|-------|
| "Total Balance" label | 14px, `textMuted` |
| Balance amount | 36px, bold, `text` |
| Change indicator | 14px, green-400 (profit) or red-400 (loss) |
| Trend icon | 16px, same color as change |

#### Chart Component

| Property | Value |
|----------|-------|
| Height | 176px |
| Type | Area chart with gradient fill |
| Line color | `theme.chartColor` |
| Line width | 2px |
| Fill | Linear gradient, top 30% opacity → bottom 0% opacity |
| X-axis | Hidden |
| Y-axis | Hidden |
| Grid | None |

#### Timeframe Selector

| Property | Value |
|----------|-------|
| Container | Centered, horizontal stack, 8px gap |
| Button padding | 20px horizontal, 8px vertical |
| Button radius | Full (pill shape) |
| Active state | `accentBg` background, black text |
| Inactive state | `card` background, `textMuted` text |
| Font | 14px, medium weight |

#### Position Card

| Property | Value |
|----------|-------|
| Background | `theme.card` |
| Border | 1px `theme.border` |
| Border radius | 16px |
| Padding | 16px |
| Margin bottom | 12px |

**Card Content:**

| Element | Style |
|---------|-------|
| Token icon | 40px circle, green-500/20 (LONG) or red-500/20 (SHORT) bg |
| Token letter | 18px bold, centered in icon |
| Token name | 16px semibold, `text` |
| Side badge | 12px, pill shape, green/red background at 20%, matching text |
| Size | 14px, `textMuted` |
| PnL amount | 16px semibold, green-400 or red-400 |
| PnL percent | 14px, green-400/70 or red-400/70 |
| Entry/Current labels | 14px, `textMuted` |
| Entry/Current values | 14px, `text` (current is medium weight) |

#### Live Indicator

| Property | Value |
|----------|-------|
| Icon | `arrow.clockwise` 12px, animated spin |
| Label | "Live" 14px |
| Color | green-400 |

---

### History Tab

Displays closed trades in expandable cards.

#### Layout Structure

```
┌────────────────────────────────────────┐
│ [Status Bar]                           │
├────────────────────────────────────────┤
│                                        │
│  Trade History              [24pt]     │
│  6 closed trades            [14pt]     │
│                                        │
├────────────────────────────────────────┤
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ↗ SOL  LONG        +$285.60   │   │  ← Collapsed
│  │   2024-01-15        +10.04%   │   │
│  │                          ▼    │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ↘ ETH  SHORT       +$175.00   │   │  ← Expanded
│  │   2024-01-14        +2.03%    │   │
│  │                          ▲    │   │
│  ├────────────────────────────────┤   │
│  │  ┌─────────┐ ┌─────────┐      │   │
│  │  │Entry    │ │Exit     │      │   │
│  │  │$3,450   │ │$3,380   │      │   │
│  │  └─────────┘ └─────────┘      │   │
│  │  ┌─────────┐ ┌─────────┐      │   │
│  │  │Size     │ │Leverage │      │   │
│  │  │2.5 ETH  │ │10x      │      │   │
│  │  └─────────┘ └─────────┘      │   │
│  │  ┌─────────┐ ┌─────────┐      │   │
│  │  │Duration │ │Fees     │      │   │
│  │  │1h 45m   │ │$8.20    │      │   │
│  │  └─────────┘ └─────────┘      │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│ [Tab Bar]                              │
└────────────────────────────────────────┘
```

#### Trade Card (Collapsed)

| Element | Style |
|---------|-------|
| Container | `card` bg, 16px radius, `border` 1px |
| Padding | 16px |
| Icon | 40px circle, trend arrow (up=LONG, down=SHORT) |
| Icon background | green-500/20 or red-500/20 |
| Token + badge | Same as position card |
| Date | 14px, `textMuted` |
| PnL | 16px semibold, green or red |
| Chevron | 20px, `textMuted`, rotates 180° when expanded |

#### Trade Card (Expanded)

Additional section below collapsed header:

| Property | Value |
|----------|-------|
| Border top | 1px `theme.border` |
| Padding | 16px top, 16px horizontal, 16px bottom |
| Grid | 2 columns, 12px gap |

**Detail Cell:**

| Property | Value |
|----------|-------|
| Background | `theme.cardHover` |
| Border radius | 12px |
| Padding | 12px |
| Label | 12px, `textMuted` |
| Value | 14px medium, `text` |

**Detail Fields:**
1. Entry Price
2. Exit Price
3. Size (with token symbol)
4. Leverage
5. Duration
6. Fees

---

### Notifications Tab

Displays trade event notifications with filtering.

#### Layout Structure

```
┌────────────────────────────────────────┐
│ [Status Bar]                           │
├────────────────────────────────────────┤
│                                        │
│  Notifications     [Mark all read]     │
│                                        │
├────────────────────────────────────────┤
│                                        │
│  ┌────────────────────────────────┐   │
│  │▌↗ Position Opened    2 min ago│   │  ← Unread (left border)
│  │    LONG ETH 2.5 @ $3,245.50   │   │
│  │    10x Leverage               │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ 🎯 Take Profit Hit!   1 hr ago│   │
│  │    ETH LONG +$342.18 (+4.2%)  │   │
│  │    Target: $3,380             │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ $ Daily Summary      3 hr ago │   │
│  │    Today: +$523.45 (+3.8%)    │   │
│  │    4 trades • 3W 1L           │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│ [Tab Bar]                              │
└────────────────────────────────────────┘
```

#### Notification Types & Icons

| Type | Icon | Icon Color | Icon BG |
|------|------|------------|---------|
| position_opened | `arrow.up.right` | blue-400 | blue-500/20 |
| position_closed | `checkmark.circle` | gray-400 | gray-500/20 |
| target_reached | `target` | green-400 | green-500/20 |
| stop_loss | `exclamationmark.triangle` | red-400 | red-500/20 |
| pnl_update | `dollarsign.circle` | purple-400 | purple-500/20 |

#### Notification Card

| Property | Value |
|----------|-------|
| Background | `theme.card` |
| Border | 1px `theme.border` |
| Border radius | 16px |
| Padding | 16px |
| Unread indicator | 4px left border, green-500 |

**Card Content:**

| Element | Style |
|---------|-------|
| Icon container | 40px circle, type-specific bg |
| Icon | 20px, type-specific color |
| Title | 16px semibold, `text` |
| Time | 12px, `textMuted` |
| Message | 14px, `text` |
| Detail | 14px, `textMuted` |

#### Mark All Read Button

| Property | Value |
|----------|-------|
| Background | `theme.accentBgSoft` |
| Text | `theme.accentText`, 14px |
| Border radius | 8px |
| Padding | 12px horizontal, 6px vertical |
| Visibility | Only shown when unreadCount > 0 |

---

### Settings Tab

#### Main Settings Screen

```
┌────────────────────────────────────────┐
│ [Status Bar]                           │
├────────────────────────────────────────┤
│                                        │
│  Settings                              │
│                                        │
├────────────────────────────────────────┤
│  NOTIFICATIONS                [label]  │
│  ┌────────────────────────────────┐   │
│  │ 🔔 Notification Preferences   ▶│   │
│  │    Choose what alerts you...   │   │
│  └────────────────────────────────┘   │
│                                        │
│  APPEARANCE                   [label]  │
│  ┌────────────────────────────────┐   │
│  │ 🎨 Theme                      ▶│   │
│  │    Midnight                    │   │
│  └────────────────────────────────┘   │
│                                        │
│  BOT STATUS                   [label]  │
│  ┌────────────────────────────────┐   │
│  │ ⚡ Bot Active              🟢 │   │
│  │    Running 14h 23m             │   │
│  └────────────────────────────────┘   │
│                                        │
│  ABOUT                        [label]  │
│  ┌────────────────────────────────┐   │
│  │ Version              1.0.0    │   │
│  │ Network         Arbitrum One  │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│ [Tab Bar]                              │
└────────────────────────────────────────┘
```

#### Settings Row Component

| Property | Value |
|----------|-------|
| Background | `theme.card` |
| Border | 1px `theme.border` |
| Border radius | 16px |
| Padding | 16px |
| Icon container | 40px, 12px radius, `accentBgSoft` or custom bg |
| Icon | 20px, `accentText` or custom color |
| Title | 16px medium, `text` |
| Subtitle | 14px, `textMuted` |
| Chevron | 20px, `textMuted` (if navigable) |

#### Section Label

| Property | Value |
|----------|-------|
| Font | 12px, uppercase, letter-spacing 0.5px |
| Color | `theme.textMuted` |
| Margin | 12px bottom, 4px left |
| Margin top | 24px (between sections) |

---

#### Notification Preferences Screen

```
┌────────────────────────────────────────┐
│ [Status Bar]                           │
├────────────────────────────────────────┤
│  ◀ Notifications                       │
├────────────────────────────────────────┤
│                                        │
│  ┌────────────────────────────────┐   │
│  │ 🔔 Push Notifications     [ON]│   │
│  │    Master toggle               │   │
│  └────────────────────────────────┘   │
│                                        │
│  ALERT TYPES                          │
│  ┌────────────────────────────────┐   │
│  │ ↗ Position Opened        [ON] │   │
│  │   When bot opens a trade       │   │
│  │   [Sound ✓] [Vibrate ✓]       │   │
│  ├────────────────────────────────┤   │
│  │ ✓ Position Closed        [ON] │   │
│  │   When a position closes       │   │
│  │   [Sound ✓] [Vibrate ✓]       │   │
│  ├────────────────────────────────┤   │
│  │ 🎯 Target Reached        [ON] │   │
│  │   When TP is hit               │   │
│  │   [Sound ✓] [Vibrate ✓]       │   │
│  ├────────────────────────────────┤   │
│  │ ⚠ Stop Loss Hit          [ON] │   │
│  │   When SL triggers             │   │
│  │   [Sound ✓] [Vibrate ✓]       │   │
│  ├────────────────────────────────┤   │
│  │ $ PnL Updates            [ON] │   │
│  │   Daily summaries              │   │
│  │   [Sound  ] [Vibrate  ]       │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│ [Tab Bar]                              │
└────────────────────────────────────────┘
```

#### Toggle Switch

| Property | Value |
|----------|-------|
| Width | 48px |
| Height | 28px |
| Border radius | 14px (full) |
| ON background | `theme.accentBg` |
| OFF background | `theme.cardHover` with 1px `border` |
| Knob | 20px circle, white |
| Knob position | 4px from edge |
| Animation | 200ms ease-out |

#### Sound/Vibrate Pills

| Property | Value |
|----------|-------|
| Padding | 12px horizontal, 6px vertical |
| Border radius | 8px |
| Font | 14px |
| Gap | 12px between pills |
| Active | `accentBgSoft` bg, `accentText` text |
| Inactive | `cardHover` bg, `textMuted` text |
| Icons | `speaker.wave.2` / `speaker.slash`, `iphone.radiowaves.left.and.right` |
| Visibility | Only shown when parent toggle is ON |

---

#### Theme Selection Screen

```
┌────────────────────────────────────────┐
│ [Status Bar]                           │
├────────────────────────────────────────┤
│  ◀ Theme                               │
├────────────────────────────────────────┤
│  All themes optimized for dark...      │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ████│ Midnight           ✓    │   │
│  │ ████│ Classic dark             │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ████│ AMOLED Black             │   │
│  │ ████│ Pure black, saves...     │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ████│ Matrix                   │   │
│  │ ████│ Cyberpunk green          │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ████│ Stealth                  │   │
│  │ ████│ Dark gray minimal        │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ████│ Ocean                    │   │
│  │ ████│ Deep blue dark           │   │
│  └────────────────────────────────┘   │
│                                        │
│  ┌────────────────────────────────┐   │
│  │ ████│ Ember                    │   │
│  │ ████│ Dark with orange...      │   │
│  └────────────────────────────────┘   │
│                                        │
├────────────────────────────────────────┤
│ [Tab Bar]                              │
└────────────────────────────────────────┘
```

#### Theme Row

| Property | Value |
|----------|-------|
| Background | `theme.card` |
| Border | 1px `theme.border`, 2px green-500 if selected |
| Border radius | 16px |
| Overflow | Hidden (for preview swatch) |
| Layout | Horizontal, swatch left + info right |

**Preview Swatch:**

| Property | Value |
|----------|-------|
| Width | 80px |
| Height | 80px |
| Background | Gradient of theme's `bg` → darker variant |
| Accent square | 32px, theme's `accentBg`, 8px radius, centered |

**Theme Info:**

| Property | Value |
|----------|-------|
| Padding | 16px |
| Name | 16px semibold, `text` |
| Description | 14px, `textMuted` |
| Checkmark | 24px circle, green-500 bg, white check (if selected) |

---

## Data Models

### Position

```swift
struct Position: Identifiable, Codable {
    let id: String
    let token: String           // "ETH", "BTC", etc.
    let side: TradeSide         // .long or .short
    let size: Double            // Amount in token units
    let entryPrice: Double
    var currentPrice: Double
    let leverage: Int
    let openedAt: Date
    
    var pnl: Double {
        let multiplier = side == .long ? 1.0 : -1.0
        return (currentPrice - entryPrice) * size * multiplier
    }
    
    var pnlPercent: Double {
        return ((currentPrice - entryPrice) / entryPrice) * 100 * (side == .long ? 1 : -1)
    }
    
    var sizeUSD: Double {
        return size * currentPrice
    }
}

enum TradeSide: String, Codable {
    case long = "LONG"
    case short = "SHORT"
}
```

### Trade (Closed)

```swift
struct Trade: Identifiable, Codable {
    let id: String
    let token: String
    let side: TradeSide
    let size: Double
    let entryPrice: Double
    let exitPrice: Double
    let leverage: Int
    let openedAt: Date
    let closedAt: Date
    let fees: Double
    
    var pnl: Double {
        let multiplier = side == .long ? 1.0 : -1.0
        return (exitPrice - entryPrice) * size * multiplier - fees
    }
    
    var pnlPercent: Double {
        return ((exitPrice - entryPrice) / entryPrice) * 100 * (side == .long ? 1 : -1)
    }
    
    var duration: String {
        let interval = closedAt.timeIntervalSince(openedAt)
        let hours = Int(interval / 3600)
        let minutes = Int((interval.truncatingRemainder(dividingBy: 3600)) / 60)
        if hours >= 24 {
            return "\(hours / 24)d \(hours % 24)h"
        }
        return "\(hours)h \(minutes)m"
    }
}
```

### Notification

```swift
struct AppNotification: Identifiable, Codable {
    let id: String
    let type: NotificationType
    let title: String
    let message: String
    let detail: String?
    let tradeId: String?
    var isRead: Bool
    let createdAt: Date
    
    var timeAgo: String {
        // Relative time formatting
    }
}

enum NotificationType: String, Codable {
    case positionOpened = "position_opened"
    case positionClosed = "position_closed"
    case targetReached = "target_reached"
    case stopLoss = "stop_loss"
    case pnlUpdate = "pnl_update"
}
```

### Notification Preferences

```swift
struct NotificationPreferences: Codable {
    var positionOpened: AlertSettings
    var positionClosed: AlertSettings
    var targetReached: AlertSettings
    var stopLoss: AlertSettings
    var pnlUpdate: AlertSettings
}

struct AlertSettings: Codable {
    var enabled: Bool
    var sound: Bool
    var vibrate: Bool
}
```

### Balance Snapshot (for charts)

```swift
struct BalanceSnapshot: Codable {
    let timestamp: Date
    let balance: Double
}
```

---

## API Endpoints

### Base URL
```
https://api.yourdomain.com
```

### WebSocket
```
wss://api.yourdomain.com/ws/live
```

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/wallet/balance` | Current wallet balance |
| GET | `/api/wallet/history?timeframe=today\|30d\|all` | Balance snapshots for chart |
| GET | `/api/positions/active` | Active positions with live PnL |
| GET | `/api/history?limit=50&offset=0` | Paginated trade history |
| GET | `/api/notifications?limit=50&offset=0` | Notification feed |
| GET | `/api/notifications/unread-count` | Badge count |
| POST | `/api/notifications/mark-read/{id}` | Mark single as read |
| POST | `/api/notifications/mark-all-read` | Mark all as read |
| GET | `/api/notifications/preferences` | Get notification settings |
| PUT | `/api/notifications/preferences` | Update notification settings |
| POST | `/api/notifications/register-device` | Register APNs token |
| GET | `/api/settings/theme` | Get current theme |
| PUT | `/api/settings/theme` | Update theme |
| GET | `/api/bot/status` | Bot running status and uptime |

### WebSocket Message Format

```json
{
  "type": "positions_update",
  "data": {
    "positions": [...],
    "prices": {"ETH": 3312.80, "BTC": 67450.00},
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

---

## Components Library

### 1. PositionCard

```swift
struct PositionCard: View {
    let position: Position
    @Environment(\.theme) var theme
    
    var body: some View {
        // Implementation based on spec above
    }
}
```

### 2. TradeCard

```swift
struct TradeCard: View {
    let trade: Trade
    @Binding var isExpanded: Bool
    @Environment(\.theme) var theme
}
```

### 3. NotificationCard

```swift
struct NotificationCard: View {
    let notification: AppNotification
    @Environment(\.theme) var theme
}
```

### 4. SettingsRow

```swift
struct SettingsRow: View {
    let icon: String
    let iconColor: Color
    let iconBackground: Color
    let title: String
    let subtitle: String
    let showChevron: Bool
    let action: () -> Void
}
```

### 5. ToggleSwitch

```swift
struct ToggleSwitch: View {
    @Binding var isOn: Bool
    @Environment(\.theme) var theme
}
```

### 6. TimeframeSelector

```swift
struct TimeframeSelector: View {
    @Binding var selected: Timeframe
    @Environment(\.theme) var theme
}

enum Timeframe: String, CaseIterable {
    case today = "Today"
    case thirtyDays = "30 Days"
    case allTime = "All Time"
}
```

### 7. ThemeRow

```swift
struct ThemeRow: View {
    let themeOption: ThemeOption
    let isSelected: Bool
    let action: () -> Void
}
```

---

## Animations & Interactions

### Transitions

| Action | Animation |
|--------|-----------|
| Tab switch | None (instant) |
| Card expand/collapse | 300ms ease-out, chevron rotates 180° |
| Toggle switch | 200ms ease-out |
| Theme change | Instant (no animation) |
| Pull to refresh | Native iOS spring |

### Gestures

| Element | Gesture | Action |
|---------|---------|--------|
| Trade card | Tap | Toggle expand/collapse |
| Position card | Tap | No action (future: detail sheet) |
| Notification | Tap | Mark as read, navigate to relevant trade |
| Tab bar | Tap | Switch tab |
| Settings row | Tap | Navigate to sub-screen |
| Theme row | Tap | Select theme |

### Haptics

| Event | Haptic |
|-------|--------|
| Tab change | Light impact |
| Toggle switch | Light impact |
| Theme select | Medium impact |
| Pull to refresh complete | Success notification |
| Error | Error notification |

---

## Typography & Spacing

### Font Sizes

| Use | Size | Weight |
|-----|------|--------|
| Screen title | 24px | Bold |
| Section header | 18px | Semibold |
| Card title | 16px | Semibold |
| Body text | 14px | Regular |
| Caption | 12px | Regular |
| Overline (labels) | 12px | Regular, uppercase |
| Tab label | 10px | Medium |
| Balance amount | 36px | Bold |
| PnL large | 16px | Semibold |
| PnL small | 14px | Regular |

### Spacing Scale

| Name | Value |
|------|-------|
| xs | 4px |
| sm | 8px |
| md | 12px |
| lg | 16px |
| xl | 20px |
| 2xl | 24px |
| 3xl | 32px |

### Common Spacings

| Element | Value |
|---------|-------|
| Screen horizontal padding | 16px |
| Card padding | 16px |
| Card margin bottom | 12px |
| Card border radius | 16px |
| Button border radius | 8px (small), 16px (large), 9999px (pill) |
| Icon container | 40px × 40px, 12px radius |
| Section gap | 24px |
| List item gap | 12px |

---

## Icons

Use SF Symbols (iOS native). Map:

| UI Element | SF Symbol |
|------------|-----------|
| Home tab | `house.fill` |
| History tab | `clock.arrow.circlepath` |
| Notifications tab | `bell.fill` |
| Settings tab | `gearshape.fill` |
| Back button | `chevron.left` |
| Expand/collapse | `chevron.down` |
| Navigate | `chevron.right` |
| Trend up | `arrow.up.right` |
| Trend down | `arrow.down.right` |
| Target | `target` |
| Warning | `exclamationmark.triangle.fill` |
| Check | `checkmark.circle.fill` |
| Dollar | `dollarsign.circle.fill` |
| Bell | `bell.fill` |
| Bell off | `bell.slash.fill` |
| Sound on | `speaker.wave.2.fill` |
| Sound off | `speaker.slash.fill` |
| Vibrate | `iphone.radiowaves.left.and.right` |
| Theme/palette | `paintpalette.fill` |
| Refresh | `arrow.clockwise` |
| Bolt (bot active) | `bolt.fill` |

---

## File Structure (iOS Project)

```
GMXTradingBot/
├── App/
│   ├── GMXTradingBotApp.swift
│   └── ContentView.swift
├── Models/
│   ├── Position.swift
│   ├── Trade.swift
│   ├── Notification.swift
│   └── Theme.swift
├── Views/
│   ├── Home/
│   │   ├── HomeView.swift
│   │   ├── BalanceHeaderView.swift
│   │   ├── ChartView.swift
│   │   ├── TimeframeSelectorView.swift
│   │   └── PositionCardView.swift
│   ├── History/
│   │   ├── HistoryView.swift
│   │   └── TradeCardView.swift
│   ├── Notifications/
│   │   ├── NotificationsView.swift
│   │   └── NotificationCardView.swift
│   ├── Settings/
│   │   ├── SettingsView.swift
│   │   ├── NotificationPreferencesView.swift
│   │   └── ThemeSelectionView.swift
│   └── Components/
│       ├── ToggleSwitch.swift
│       ├── SettingsRow.swift
│       └── PillButton.swift
├── ViewModels/
│   ├── HomeViewModel.swift
│   ├── HistoryViewModel.swift
│   ├── NotificationsViewModel.swift
│   └── SettingsViewModel.swift
├── Services/
│   ├── APIClient.swift
│   ├── WebSocketManager.swift
│   └── NotificationManager.swift
├── Theme/
│   ├── ThemeManager.swift
│   └── Themes.swift
└── Resources/
    └── Assets.xcassets
```

---

## Implementation Checklist

### Phase 1: Foundation
- [ ] Create Xcode project with SwiftUI
- [ ] Set up theme system with all 6 themes
- [ ] Create ThemeManager as @Observable
- [ ] Build tab bar navigation
- [ ] Create base components (ToggleSwitch, SettingsRow, etc.)

### Phase 2: Home Screen
- [ ] Balance header component
- [ ] Swift Charts integration for area chart
- [ ] Timeframe selector
- [ ] Position card component
- [ ] WebSocket connection for live updates

### Phase 3: History Screen
- [ ] Trade card with expand/collapse
- [ ] API integration for trade history
- [ ] Pull to refresh

### Phase 4: Notifications
- [ ] Notification card component
- [ ] Unread badge on tab
- [ ] Mark as read functionality
- [ ] APNs registration

### Phase 5: Settings
- [ ] Main settings screen
- [ ] Notification preferences screen
- [ ] Theme selection screen
- [ ] Persist preferences to backend

### Phase 6: Polish
- [ ] Add haptics
- [ ] Test all themes
- [ ] Handle loading and error states
- [ ] Add pull to refresh everywhere
- [ ] TestFlight deployment

---

## Notes

- The bot handles all trade execution automatically — this app is for monitoring only
- Manual trade controls (close position, partial close, etc.) are deferred for future phases
- WebSocket provides real-time position updates every 2 seconds
- All times displayed in user's local timezone
- Currency is always USD
- Network is always Arbitrum One

---

**End of Specification**