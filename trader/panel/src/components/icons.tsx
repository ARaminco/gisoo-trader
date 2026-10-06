/* Inline icons so the app has no icon-font dependency and works offline. */

export type IconProps = { className?: string; strokeWidth?: number }

function Svg({
  className,
  strokeWidth = 1.9,
  children,
}: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

export const HomeIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 11.5 12 4l9 7.5" />
    <path d="M5 10v10h5v-6h4v6h5V10" />
  </Svg>
)

export const TradesIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 7h13" />
    <path d="m14 4 3 3-3 3" />
    <path d="M20 17H7" />
    <path d="m10 14-3 3 3 3" />
  </Svg>
)

export const SignalIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 12h4l3-8 4 16 3-8h6" />
  </Svg>
)

export const WalletIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M19 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5" />
    <circle cx="16" cy="13" r="1.4" fill="currentColor" stroke="none" />
  </Svg>
)

export const MoreIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="4" y="4" width="6" height="6" rx="1.5" />
    <rect x="14" y="4" width="6" height="6" rx="1.5" />
    <rect x="4" y="14" width="6" height="6" rx="1.5" />
    <rect x="14" y="14" width="6" height="6" rx="1.5" />
  </Svg>
)

export const FlaskIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 2v6.5L4.5 18A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-3L14 8.5V2" />
    <path d="M9 2h6" />
    <path d="M7 15h10" />
  </Svg>
)

export const GearIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 7a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9 2.6h.1A2 2 0 1 1 13 3v.1A1.7 1.7 0 0 0 17 4.6a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.6 1h.1a2 2 0 1 1 0 4H21a1.7 1.7 0 0 0-1.6 1z" />
  </Svg>
)

export const BookIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    <path d="M9 7h7" />
    <path d="M9 11h5" />
  </Svg>
)

export const ShieldIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 2.5 4.5 5.5v6c0 4.6 3.2 8.6 7.5 10 4.3-1.4 7.5-5.4 7.5-10v-6z" />
    <path d="m9 12 2 2 4-4" />
  </Svg>
)

export const BellIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z" />
    <path d="M10 20.5a2 2 0 0 0 4 0" />
    <path d="M12 3v2" />
  </Svg>
)

/** Mirrored for RTL: the door sits on the start (right) side, the arrow leaves toward the left. */
export const LogoutIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M15 21h4a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-4" />
    <path d="m8 17-5-5 5-5" />
    <path d="M3 12h12" />
  </Svg>
)

/** Points toward the "back" direction in an RTL layout. */
export const BackIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m9 6 6 6-6 6" />
  </Svg>
)

/** Points toward "forward / open" in an RTL layout. */
export const ChevronIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m15 6-6 6 6 6" />
  </Svg>
)

export const CloseIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </Svg>
)

export const CopyIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </Svg>
)

export const CheckIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  </Svg>
)

export const AlertIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3 2.5 19.5h19z" />
    <path d="M12 10v4" />
    <path d="M12 17.5h.01" />
  </Svg>
)

export const WifiOffIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m2 2 20 20" />
    <path d="M8.5 16.5a5 5 0 0 1 7 0" />
    <path d="M5 12.5a10 10 0 0 1 4.5-2.6" />
    <path d="M14.5 9.9A10 10 0 0 1 19 12.5" />
    <path d="M2 8.8a15 15 0 0 1 3-2" />
    <path d="M12 5c3.6 0 7 1.3 10 3.8" />
    <path d="M12 20h.01" />
  </Svg>
)

export const DownloadIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v12" />
    <path d="m7 10 5 5 5-5" />
    <path d="M4 20h16" />
  </Svg>
)

export const ShareIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v12" />
    <path d="m8 7 4-4 4 4" />
    <path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />
  </Svg>
)

export const StopIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M7.5 3h9l4.5 4.5v9L16.5 21h-9L3 16.5v-9z" />
    <path d="M12 8v4.5" />
    <path d="M12 16h.01" />
  </Svg>
)

export const PowerIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v9" />
    <path d="M6.3 6.3a8 8 0 1 0 11.4 0" />
  </Svg>
)

export const EyeIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z" />
    <circle cx="12" cy="12" r="2.8" />
  </Svg>
)

export const EyeOffIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m3 3 18 18" />
    <path d="M10.6 6.1A10 10 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-3.2 3.9" />
    <path d="M6.6 6.6C3.7 8.5 2 12 2 12s3.5 6 10 6c1.4 0 2.7-.3 3.8-.7" />
    <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
  </Svg>
)

export const RefreshIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
    <path d="M3 3v5h5" />
  </Svg>
)

export const KeyIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="8" cy="15" r="4" />
    <path d="m10.9 12.1 8.6-8.6" />
    <path d="m16 6 3 3" />
    <path d="m13 9 3 3" />
  </Svg>
)

/** A hardware security key (FIDO2 / passkey). */
export const PasskeyIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2.5" y="8" width="12.5" height="8" rx="2.5" />
    <path d="M15 10.5h4.5a1 1 0 0 1 1 1v1a1 1 0 0 1-1 1H15" />
    <path d="M18 10.5v3" />
    <circle cx="8" cy="12" r="1.4" fill="currentColor" stroke="none" />
  </Svg>
)

export const TrashIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 7h16" />
    <path d="M9.5 7V4.5h5V7" />
    <path d="m6 7 .9 12.2A1 1 0 0 0 7.9 20h8.2a1 1 0 0 0 1-.8L18 7" />
    <path d="M10 11v5.5" />
    <path d="M14 11v5.5" />
  </Svg>
)

export const LockIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="5" y="11" width="14" height="10" rx="2" />
    <path d="M8 11V7a4 4 0 0 1 8 0v4" />
  </Svg>
)

export const SparkIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v3" />
    <path d="M12 18v3" />
    <path d="M3 12h3" />
    <path d="M18 12h3" />
    <path d="m5.6 5.6 2.2 2.2" />
    <path d="m16.2 16.2 2.2 2.2" />
    <path d="m5.6 18.4 2.2-2.2" />
    <path d="m16.2 7.8 2.2-2.2" />
  </Svg>
)

/** The investment committee: a table with people around it. */
export const CommitteeIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="6" r="2.2" />
    <circle cx="5" cy="9.5" r="2" />
    <circle cx="19" cy="9.5" r="2" />
    <path d="M9.4 12.6A3.6 3.6 0 0 1 14.6 12.6" />
    <path d="M3 14.4a2.6 2.6 0 0 1 4 0" />
    <path d="M17 14.4a2.6 2.6 0 0 1 4 0" />
    <rect x="6" y="16" width="12" height="4" rx="1.4" />
  </Svg>
)
