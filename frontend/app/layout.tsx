import type { Metadata } from 'next'
import { Space_Grotesk, Fira_Code } from 'next/font/google'
import './globals.css'

const spaceGrotesk = Space_Grotesk({
  subsets: ['latin'],
  weight: ['300', '400', '500', '600', '700'],
  variable: '--font-space-grotesk',
  display: 'swap',
})

const firaCode = Fira_Code({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-fira-code',
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'ScarTissue — Every codebase remembers its bugs.',
  description: 'PR review agent that warns about reintroduced bug patterns',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${spaceGrotesk.variable} ${firaCode.variable}`}>
      <body>{children}</body>
    </html>
  )
}
