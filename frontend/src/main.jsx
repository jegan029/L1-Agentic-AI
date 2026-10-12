import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

// The typeface. By default nothing is requested from another origin: Inter is used
// where it is installed and the system sans-serif elsewhere. A build that sets
// VITE_FONT_STYLESHEET loads that stylesheet, and the server that hosts the page has
// to allow its origin (UI_STYLE_SRC and UI_FONT_SRC in the dashboard image).
const fontStylesheet = import.meta.env.VITE_FONT_STYLESHEET
if (fontStylesheet) {
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = fontStylesheet
  document.head.appendChild(link)
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
