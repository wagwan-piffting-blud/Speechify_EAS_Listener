<?xml version="1.0"?>
<!-- Copyright (c) 2001-2003 SpeechWorks International, Inc. -->
<!-- All Rights Reserved -->

<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output method="html"/>

<xsl:template match="/">
  <html>
    <head>
      <title>SpeechWorks Speechify Configuration File</title>
    </head>
    <body bgcolor="#ffe4bf">
      <xsl:apply-templates select="//SWIttsConfig"/>
    </body>
  </html>
</xsl:template>


<xsl:template match="SWIttsConfig">
  <h1>Speechify&#8482; Configuration File</h1>
  <p> 
    Copyright &#169; 2001-2003 SpeechWorks International, Inc. <br/>
    All Rights Reserved
  </p>

  <p>
    <a href="http://techsupport.speechworks.com">
      SpeechWorks Technical Support
    </a>
  </p>

  <xsl:apply-templates/>
</xsl:template>

<xsl:template match="lang">
  <h1>Language <xsl:value-of select="@name"/></h1>
  <table border="5" cellspacing="2" cellpadding="1">
    <tr>
      <td><center><em>Parameter</em></center></td>
      <td><center><em>Value</em></center></td>
    </tr>
    <xsl:apply-templates/>
  </table>
</xsl:template>

<xsl:template match="param">
  <tr>
    <td><xsl:value-of select="@name"/></td>
    <td><xsl:apply-templates/></td>
  </tr>
</xsl:template>

<xsl:template match="value">
  <xsl:value-of select="text()"/> 
</xsl:template>

<xsl:template match="namedValue">
  <xsl:value-of select="@name"/>=<xsl:value-of select="text()"/> 
</xsl:template>

</xsl:stylesheet>
