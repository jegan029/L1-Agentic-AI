export default function StateStreetLogo({ height = 40 }) {
  const w = height * 4.4
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 220 50"
      width={w}
      height={height}
      style={{ display: 'block', flexShrink: 0 }}
      aria-label="State Street"
    >
      {/* Blue square mark */}
      <rect x="0" y="1" width="48" height="48" rx="2" fill="#001AFF" />

      {/* "SS" initials in white */}
      <text
        x="24" y="33"
        textAnchor="middle"
        fontFamily="Arial, Helvetica, sans-serif"
        fontWeight="900"
        fontSize="24"
        fill="#ffffff"
        letterSpacing="-1"
      >SS</text>

      {/* Vertical divider */}
      <line x1="62" y1="8" x2="62" y2="42" stroke="#dde0ec" strokeWidth="1" />

      {/* "STATE STREET" wordmark */}
      <text
        x="72" y="25"
        fontFamily="Arial, Helvetica, sans-serif"
        fontWeight="700"
        fontSize="16"
        fill="#020B5B"
        letterSpacing="1"
      >STATE STREET</text>

      {/* Sub-label */}
      <text
        x="73" y="40"
        fontFamily="Arial, Helvetica, sans-serif"
        fontWeight="400"
        fontSize="10"
        fill="#3a4470"
        letterSpacing="2"
      >CORPORATION</text>
    </svg>
  )
}
